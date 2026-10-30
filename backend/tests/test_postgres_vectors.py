"""Integration tests use a dedicated, disposable schema on a real PostgreSQL."""

import importlib
import importlib.util
import os
import uuid

import pytest

psycopg = pytest.importorskip('psycopg')
pytest.importorskip('pgvector')


@pytest.fixture
def pg_connection():
    if os.environ.get('TAWZEEF_TEST_POSTGRES') != '1':
        pytest.skip('Requires an explicitly configured PostgreSQL test database')
    from psycopg import sql

    schema = 'tawzeef_test_' + uuid.uuid4().hex
    with psycopg.connect(autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        conn.execute(sql.SQL('SET search_path TO {}, public').format(sql.Identifier(schema)))
        try:
            yield conn
        finally:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_allowed_and_current_sources_only_are_returned(pg_connection):
    # Removing the permitted-source join or ignoring content revisions must fail.
    assert importlib.util.find_spec('app.postgres_vectors') is not None
    module = importlib.import_module('app.postgres_vectors')
    index = module.PostgresVectorIndex(pg_connection, 'fixture')
    index.initialize()
    index.replace([
        ('public', 'a' * 64, [1.0] + [0.0] * 383),
        ('private', 'b' * 64, [1.0] + [0.0] * 383),
        ('different', 'c' * 64, [0.0, 1.0] + [0.0] * 382),
    ])
    assert [row['source_id'] for row in index.search(
        {'public': 'a' * 64, 'different': 'c' * 64}, [1.0] + [0.0] * 383,
    )] == ['public', 'different']
    assert index.search({'public': 'd' * 64}, [1.0] + [0.0] * 383) == []
    assert index.search({}, [1.0] + [0.0] * 383) == []
    index.replace([('different', 'c' * 64, [0.0, 1.0] + [0.0] * 382)])
    assert index.search({'public': 'a' * 64}, [1.0] + [0.0] * 383) == []


def test_collection_isolation_and_failed_update_preserves_index(pg_connection):
    from app.postgres_vectors import PostgresVectorIndex

    first = PostgresVectorIndex(pg_connection, 'one')
    first.initialize()
    other = PostgresVectorIndex(pg_connection, 'two')
    vector = [1.0] + [0.0] * 383
    first.replace([('source', 'a' * 64, vector)])
    other.replace([('source', 'b' * 64, vector)])
    with pytest.raises(ValueError):
        first.replace([('source', 'c' * 64, [0.0] * 384)])
    assert [row['source_id'] for row in first.search({'source': 'a' * 64}, vector)] == ['source']
    assert first.search({'source': 'b' * 64}, vector) == []
    assert [row['source_id'] for row in other.search({'source': 'b' * 64}, vector)] == ['source']
    pg_connection.execute("ALTER TABLE tawzeef_vectors ADD CONSTRAINT test_reject CHECK (source_id <> 'explode')")
    with pytest.raises(psycopg.errors.CheckViolation):
        first.replace([('source', 'c' * 64, vector), ('explode', 'd' * 64, vector)])
    assert [row['source_id'] for row in first.search({'source': 'a' * 64}, vector)] == ['source']
    assert first.search({'source': 'c' * 64}, vector) == []
    first.replace([])
    assert first.search({'source': 'a' * 64}, vector) == []
    assert [row['source_id'] for row in other.search({'source': 'b' * 64}, vector)] == ['source']


def test_pg_search_only_embeds_authorized_and_rechecks_revocation(tmp_path, pg_connection):
    np = pytest.importorskip('numpy')
    pytest.importorskip('onnxruntime')
    pytest.importorskip('tokenizers')
    pytest.importorskip('sqlite_vec')
    from app.db import connect, initialize
    from app.knowledge_documents import initialize_knowledge_documents

    assert importlib.util.find_spec('app.postgres_retrieval') is not None
    module = importlib.import_module('app.postgres_retrieval')
    path = tmp_path / 'sources.sqlite3'
    initialize(path)
    initialize_knowledge_documents(path)
    with connect(path) as db:
        hr = db.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES('hr','HR','hr','x')").lastrowid
        public = db.execute("INSERT INTO knowledge_documents(title,audience,text,created_by) VALUES('Leave','public','Annual leave is 21 days',?)", (hr,)).lastrowid
        db.execute("INSERT INTO knowledge_documents(title,audience,text,created_by) VALUES('Secret','hr','PRIVATE_UNAUTHORIZED',?)", (hr,))

    class Embedder:
        def __init__(self, callback=None):
            self.seen = []
            self.callback = callback

        def encode(self, texts, *, query):
            self.seen.extend(texts)
            if not query and self.callback:
                self.callback()
                self.callback = None
            return np.array([[1.0] + [0.0] * 383 for _ in texts], dtype=np.float32)

    embedder = Embedder()
    answer = module.search_postgres(path, 'candidate', 'annual leave days', embedder, pg_connection)
    assert [source['document_id'] for source in answer['sources']] == [public]
    assert 'PRIVATE_UNAUTHORIZED' not in str(embedder.seen)
    assert 'PRIVATE_UNAUTHORIZED' not in str(answer)

    def deactivate():
        with connect(path) as db:
            db.execute('UPDATE knowledge_documents SET active = 0 WHERE id = ?', (public,))

    answer = module.search_postgres(path, 'candidate', 'annual leave days', Embedder(deactivate), pg_connection)
    assert answer['sources'] == [] and answer['status'] == 'abstain'


def test_collection_filter_limits_model_and_preserves_other_namespaces(tmp_path, pg_connection):
    np = pytest.importorskip('numpy')
    from app.db import connect, initialize
    from app.knowledge_documents import initialize_knowledge_documents
    from app.postgres_retrieval import search_postgres

    path = tmp_path / 'collections.sqlite3'
    initialize(path)
    initialize_knowledge_documents(path)
    with connect(path) as db:
        hr = db.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES('hr','HR','hr','x')").lastrowid
        handbook = db.execute("INSERT INTO knowledge_documents(title,audience,collection,text,created_by) VALUES('Handbook','public','handbook','LEAVE_HANDBOOK',?)", (hr,)).lastrowid
        job = db.execute("INSERT INTO knowledge_documents(title,audience,collection,text,created_by) VALUES('Job','public','job_description','LEAVE_JOB',?)", (hr,)).lastrowid
        # Defense against inconsistent audience even if records are tampered with.
        db.execute("INSERT INTO knowledge_documents(title,audience,collection,text,created_by) VALUES('Private','public','past_hire_patterns','LEAVE_PRIVATE',?)", (hr,))

    class Embedder:
        def __init__(self, callback=None):
            self.seen = []
            self.callback = callback

        def encode(self, texts, *, query):
            if not query:
                self.seen.extend(texts)
                if self.callback:
                    self.callback()
                    self.callback = None
            return np.array([[1.0] + [0.0] * 383 for _ in texts], dtype=np.float32)

    all_docs = search_postgres(path, 'employee', 'leave', Embedder(), pg_connection)
    assert {s['document_id'] for s in all_docs['sources']} == {handbook, job}
    all_namespace = pg_connection.execute('SELECT DISTINCT namespace FROM tawzeef_vectors').fetchone()[0]
    embedder = Embedder()
    filtered = search_postgres(path, 'employee', 'leave', embedder, pg_connection, collection='handbook')
    assert [s['document_id'] for s in filtered['sources']] == [handbook]
    assert filtered['sources'][0]['collection'] == 'handbook'
    assert embedder.seen == ['LEAVE_HANDBOOK']
    assert pg_connection.execute('SELECT count(*) FROM tawzeef_vectors WHERE namespace=%s', (all_namespace,)).fetchone()[0] == 2
    job_only = search_postgres(path, 'employee', 'leave', Embedder(), pg_connection, collection='job_description')
    assert [s['document_id'] for s in job_only['sources']] == [job]
    private = Embedder()
    assert search_postgres(path, 'employee', 'leave', private, pg_connection, collection='past_hire_patterns')['sources'] == []
    assert private.seen == []
    with pytest.raises(ValueError):
        search_postgres(path, 'employee', 'leave', Embedder(), pg_connection, collection='invalid')

    def change_collection():
        with connect(path) as db:
            db.execute("UPDATE knowledge_documents SET collection='job_description' WHERE id=?", (handbook,))

    stale = search_postgres(path, 'employee', 'leave', Embedder(change_collection), pg_connection, collection='handbook')
    assert stale['sources'] == []


def test_job_filter_uses_separate_namespace_and_rechecks_source(tmp_path, pg_connection):
    np = pytest.importorskip('numpy')
    from app.db import connect, initialize
    from app.knowledge_documents import initialize_knowledge_documents
    from app.postgres_retrieval import search_postgres

    path = tmp_path / 'jobs.sqlite3'
    initialize(path)
    initialize_knowledge_documents(path)
    with connect(path) as db:
        hr = db.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES('hr','HR','hr','x')").lastrowid
        for job in ('job-one', 'job-two'):
            db.execute("INSERT INTO jobs(id,title,location,required_skills,preferred_skills,responsibilities) VALUES(?,?,?,'[]','[]','[]')", (job, job, 'Remote'))
        for job, body in [('job-one', 'ROLE_ONE'), ('job-two', 'ROLE_TWO')]:
            db.execute("INSERT INTO knowledge_documents(title,audience,collection,text,created_by,job_id) VALUES(?, 'public', 'job_description', ?, ?, ?)", (job, body, hr, job))
        db.execute("INSERT INTO knowledge_documents(title,audience,collection,text,created_by,job_id) VALUES('Private', 'public', 'past_hire_patterns', 'ROLE_PRIVATE', ?, 'job-one')", (hr,))

    class Embedder:
        def __init__(self, callback=None):
            self.seen = []
            self.callback = callback

        def encode(self, texts, *, query):
            if not query:
                self.seen.extend(texts)
                if self.callback:
                    self.callback()
                    self.callback = None
            return np.array([[1.0] + [0.0] * 383 for _ in texts], dtype=np.float32)

    all_docs = search_postgres(path, 'employee', 'role', Embedder(), pg_connection, collection='job_description')
    assert {r['job_id'] for r in all_docs['sources']} == {'job-one', 'job-two'}
    prior = pg_connection.execute('SELECT namespace,count(*) FROM tawzeef_vectors GROUP BY namespace').fetchall()
    embedder = Embedder()
    result = search_postgres(path, 'employee', 'role', embedder, pg_connection, collection='job_description', job_id='job-one')
    assert [r['text'] for r in result['sources']] == ['ROLE_ONE']
    assert embedder.seen == ['ROLE_ONE']
    for name, count in prior:
        assert pg_connection.execute('SELECT count(*) FROM tawzeef_vectors WHERE namespace=%s', (name,)).fetchone()[0] == count
    assert search_postgres(path, 'employee', 'role', Embedder(), pg_connection, job_id='unknown')['sources'] == []
    assert search_postgres(path, 'candidate', 'role', Embedder(), pg_connection, collection='past_hire_patterns', job_id='job-one')['sources'] == []

    def reassign():
        with connect(path) as db:
            db.execute("UPDATE knowledge_documents SET job_id='job-two' WHERE title='job-one'")

    changed = search_postgres(path, 'employee', 'role', Embedder(reassign), pg_connection, collection='job_description', job_id='job-one')
    assert changed['sources'] == []


def test_hybrid_api_uses_session_role_with_real_model_and_postgres(tmp_path, pg_connection, monkeypatch):
    from pathlib import Path

    pytest.importorskip('onnxruntime')
    pytest.importorskip('tokenizers')
    model_dir = Path(__file__).resolve().parents[2] / '.tooling' / 'multilingual-e5-small'
    if not (model_dir / 'model.onnx').is_file() or not (model_dir / 'tokenizer.json').is_file():
        pytest.skip('Requires the pinned local model files')
    assert importlib.util.find_spec('app.hybrid_api') is not None
    from fastapi.testclient import TestClient

    from app.db import connect
    from app.main import create_app

    original_port = os.environ.get('PGPORT', '5432')
    schema = pg_connection.execute('SELECT current_schema()').fetchone()[0]
    monkeypatch.setenv('PGOPTIONS', f'-c search_path={schema},public')
    monkeypatch.setenv('TAWZEEF_ENABLE_HYBRID', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', str(model_dir))
    path = tmp_path / 'api.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        url = '/api/knowledge/experimental-hybrid'
        assert client.post(url, json={'question': 'annual leave days'}).status_code == 401
        with connect(path) as db:
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            db.execute("INSERT INTO knowledge_documents(title,audience,text,created_by,job_id) VALUES('Annual leave','public','Employees receive 21 days of annual leave each year.',?,'job-backend-python')", (hr,))
            db.execute("INSERT INTO knowledge_documents(title,audience,text,created_by) VALUES('Private leave','hr','SECRET HR leave policy: 999 days.',?)", (hr,))
            db.execute("INSERT INTO knowledge_documents(title,audience,collection,text,created_by) VALUES('Private patterns','public','past_hire_patterns','SECRET HISTORICAL leave notes',?)", (hr,))
        assert client.post('/api/auth/login', json={
            'username': 'candidate', 'password': 'candidate-demo-only',
        }).status_code == 200
        response = client.post(url, json={'question': 'How many days of annual leave do employees get?'})
        assert response.status_code == 200
        assert response.json()['sources'][0]['title'] == 'Annual leave'
        assert 'SECRET HR' not in response.text
        filtered = client.post(url, json={'question': 'How many days of annual leave do employees get?', 'job_id': 'job-backend-python'})
        assert filtered.status_code == 200 and filtered.json()['sources'][0]['job_id'] == 'job-backend-python'
        unknown = client.post(url, json={'question': 'leave', 'job_id': 'unknown-job'})
        assert unknown.status_code == 200 and unknown.json()['sources'] == []
        assert client.post(url, json={'question': 'leave', 'job_id': 'invalid id'}).status_code == 422
        assert client.post(url, json={'question': 'leave', 'collection': 'bad'}).status_code == 422
        restricted = client.post(url, json={'question': 'leave', 'collection': 'past_hire_patterns'})
        assert restricted.status_code == 200
        assert restricted.json()['sources'] == []
        assert 'SECRET HISTORICAL' not in restricted.text
        monkeypatch.setenv('TAWZEEF_USE_HYBRID_ANSWERS', '1')
        combined = client.post('/api/knowledge/answer', json={
            'question': 'How many days of annual leave do employees get?',
        })
        assert combined.status_code == 200
        assert any(source['title'] == 'Annual leave' for source in combined.json()['sources'])
        assert 'hybrid' in combined.json()['notice'].lower()
        assert 'SECRET HR' not in combined.text
        monkeypatch.setenv('PGPORT', '1')
        unavailable = client.post(url, json={'question': 'How many days of annual leave do employees get?'})
        assert unavailable.status_code == 503
        assert unavailable.json()['detail'] == 'Experimental hybrid search is unavailable'
        monkeypatch.setenv('PGPORT', original_port)

        class RevokeDuringEmbedding:
            def encode(self, texts, *, query):
                if not query:
                    with connect(path) as db:
                        db.execute("DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE username='candidate')")
                return [[1.0] + [0.0] * 383 for _ in texts]

        from app import hybrid_api

        monkeypatch.setattr(hybrid_api, '_load_model', lambda directory: RevokeDuringEmbedding())
        revoked = client.post(url, json={'question': 'How many days of annual leave do employees get?'})
        assert revoked.status_code == 401
        assert 'Employees receive' not in revoked.text

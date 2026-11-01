"""Opt-in integration test against a disposable PostgreSQL and local model."""
import os
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

psycopg = pytest.importorskip('psycopg')
pytest.importorskip('pgvector')
pytest.importorskip('onnxruntime')
pytest.importorskip('tokenizers')


@pytest.fixture
def isolated_vectors(monkeypatch):
    if os.environ.get('TAWZEEF_TEST_POSTGRES') != '1':
        pytest.skip('Requires a dedicated disposable PostgreSQL test database')
    from psycopg import sql

    schema = 'tawzeef_policy_' + uuid.uuid4().hex
    with psycopg.connect(autocommit=True) as connection:
        database = connection.execute('SELECT current_database()').fetchone()[0]
        if not re.fullmatch(r'tawzeef_test_[a-f0-9]{32}', database):
            raise RuntimeError('PostgreSQL database is not a dedicated disposable test database')
        connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        try:
            connection.execute(sql.SQL('''CREATE TABLE {}.tawzeef_vectors (
                namespace text NOT NULL, model text NOT NULL, source_id text NOT NULL,
                revision text NOT NULL CHECK (length(revision)=64),
                embedding public.vector(384) NOT NULL, PRIMARY KEY (namespace,model,source_id)
            )''').format(sql.Identifier(schema)))
            monkeypatch.setenv('PGOPTIONS', f'-c search_path={schema},public')
            with psycopg.connect(autocommit=True) as probe:
                assert probe.execute('SELECT current_schema()').fetchone()[0] == schema
            yield
        finally:
            connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_real_policy_suggestions_are_scoped_unverified_and_revocable(tmp_path, isolated_vectors, monkeypatch):
    from app.db import connect
    from app.main import create_app

    model_dir = Path(__file__).resolve().parents[2] / '.tooling' / 'multilingual-e5-small'
    if not (model_dir / 'model.onnx').is_file() or not (model_dir / 'tokenizer.json').is_file():
        pytest.skip('Requires the pinned local model files')
    monkeypatch.setenv('TAWZEEF_ENABLE_HYBRID', '1')
    monkeypatch.setenv('TAWZEEF_POLICY_HYBRID_SUGGESTIONS', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', str(model_dir))
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '0')
    monkeypatch.delenv('TAWZEEF_USE_POLICY_HYBRID', raising=False)
    path = tmp_path / 'policy.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        def login(role):
            assert client.post('/api/auth/login', json={
                'username': role, 'password': f'{role}-demo-only',
            }).status_code == 200

        login('hr')
        response = client.post('/api/knowledge/documents/text', json={
            'title': 'Remote work', 'text': 'Staff may work remotely two days per week with manager approval.',
            'audience': 'staff', 'collection': 'handbook',
        })
        assert response.status_code == 201
        doc_id = response.json()['id']
        with connect(path) as db:
            db.execute('UPDATE knowledge_settings SET include_sample_policies=0 WHERE id=1')
        login('employee')
        conversation = client.post('/api/conversations', json={'title': 'Policy'}).json()['id']
        endpoint = f'/api/conversations/{conversation}/messages'
        response = client.post(endpoint, json={'question': 'Can employees work from home?', 'mode': 'policy'})
        assert response.status_code == 201
        body = response.json()
        assert body['mode'] == 'abstain'
        assert 'not an answer' in body['answer'] and 'unverified' in body['notice']
        assert len(body['sources']) == 1 and body['sources'][0]['id'].startswith(f'db-doc-{doc_id}-')
        with connect(path) as db:
            db.execute("UPDATE knowledge_documents SET audience='hr' WHERE id=?", (doc_id,))
        history = client.get(f'/api/conversations/{conversation}').json()['messages'][0]
        assert history['mode'] == 'unavailable' and history['sources'] == []
        response = client.post(endpoint, json={'question': 'Can employees work from home?', 'mode': 'policy'})
        assert response.status_code == 201 and response.json()['sources'] == []

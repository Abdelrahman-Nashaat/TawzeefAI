"""Synthetic end-to-end tests against an isolated real PostgreSQL schema."""

import io
import os
import uuid

import pytest
from docx import Document
from fastapi.testclient import TestClient

psycopg = pytest.importorskip('psycopg')
from psycopg import sql
from psycopg.rows import dict_row

from app.main import create_app


@pytest.fixture
def mode(monkeypatch):
    dsn = os.environ.get('TAWZEEF_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('Set TAWZEEF_TEST_POSTGRES_DSN to a disposable PostgreSQL database')
    schema = 'tawzeef_exp_' + uuid.uuid4().hex
    monkeypatch.setenv('TAWZEEF_DATABASE_ENGINE', 'postgres-experimental')
    monkeypatch.setenv('TAWZEEF_POSTGRES_DSN', dsn)
    monkeypatch.setenv('TAWZEEF_POSTGRES_SCHEMA', schema)
    # Any accidental SQLite path in this mode is a test failure.
    import app.db
    monkeypatch.setattr(app.db.sqlite3, 'connect', lambda *args, **kwargs: pytest.fail('SQLite was opened'))
    try:
        yield dsn, schema
    finally:
        with psycopg.connect(dsn, autocommit=True) as db:
            db.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(schema)))


def db_for(mode):
    dsn, schema = mode
    db = psycopg.connect(dsn, row_factory=dict_row)
    db.execute(sql.SQL('SET search_path TO {}, pg_catalog').format(sql.Identifier(schema)))
    return db


def resume_bytes():
    doc = Document()
    doc.add_paragraph('Synthetic candidate built Python services.')
    data = io.BytesIO()
    doc.save(data)
    return data.getvalue()


def login(client, username):
    result = client.post('/api/auth/login', json={'username':username, 'password':username + '-demo-only'})
    assert result.status_code == 200, result.text


def test_candidate_submission_and_hr_read_are_postgresql_only(mode):
    app = create_app(seed_demo=True)
    with TestClient(app) as candidate, TestClient(app) as other, TestClient(app) as hr:
        assert candidate.get('/api/health').json()['mode'] == 'postgres-experimental-submission-only'
        assert candidate.get('/api/jobs').status_code == 401
        login(candidate, 'candidate')
        jobs = candidate.get('/api/jobs').json()
        assert jobs
        response = candidate.post('/api/applications', params={'job_id':jobs[0]['id'], 'filename':'resume.docx'},
                                  content=resume_bytes())
        assert response.status_code == 201, response.text
        item = response.json()
        assert item['status'] == 'submitted' and 'resume_text' not in item
        assert len(candidate.get('/api/applications').json()) == 1
        login(other, 'candidate2')
        assert other.get('/api/applications').json() == []
        assert other.post('/api/applications/1/decision', json={'decision':'hire'}).status_code == 404
        login(hr, 'hr')
        assert hr.post('/api/applications', params={'job_id':jobs[0]['id'], 'filename':'r.docx'}, content=resume_bytes()).status_code == 403
        assert 'Synthetic candidate' in hr.get('/api/applications').json()[0]['resume_text']
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM applications').fetchone()['n'] == 1
            assert db.execute('SELECT count(*) AS n FROM hr_notifications').fetchone()['n'] == 1
            assert db.execute('SELECT count(*) AS n FROM audit_events').fetchone()['n'] == 1
            assert db.execute('SELECT count(*) AS n FROM provisional_snapshots').fetchone()['n'] == 1
    # Existing data survives a clean restart and is never re-seeded.
    with TestClient(create_app(seed_demo=True)) as client:
        login(client, 'candidate')
        assert len(client.get('/api/applications').json()) == 1


def test_invalid_inputs_cross_origin_and_transaction_rollback(mode):
    app = create_app(seed_demo=True)
    with TestClient(app, raise_server_exceptions=False) as client:
        login(client, 'candidate')
        job = client.get('/api/jobs').json()[0]['id']
        assert client.post('/api/applications', params={'job_id':job,'filename':'r.docx'},
                           content=resume_bytes(), headers={'origin':'https://invalid.example'}).status_code == 403
        assert client.post('/api/applications', params={'job_id':'missing','filename':'r.docx'}, content=resume_bytes()).status_code == 404
        assert client.post('/api/applications', params={'job_id':job,'filename':'r.docx','ocr':'true'}, content=resume_bytes()).status_code == 422
        assert client.post('/api/applications', params={'job_id':job,'filename':'r.docx'}, content=b'broken').status_code == 422
        with db_for(mode) as db:
            db.execute("ALTER TABLE provisional_snapshots ADD CONSTRAINT reject_pending CHECK (snapshot_kind <> 'pending')")
        assert client.post('/api/applications', params={'job_id':job,'filename':'r.docx'}, content=resume_bytes()).status_code == 500
        with db_for(mode) as db:
            for table in ('applications','hr_notifications','audit_events','provisional_snapshots'):
                assert db.execute(sql.SQL('SELECT count(*) AS n FROM {}').format(sql.Identifier(table))).fetchone()['n'] == 0


def test_revoked_session_cannot_submit_after_document_processing(mode, monkeypatch):
    import app.postgres_experimental as pg_app

    with TestClient(create_app(seed_demo=True)) as client:
        login(client, 'candidate')
        job = client.get('/api/jobs').json()[0]['id']
        original = pg_app.extract_document_text
        def revoke(filename, content):
            result = original(filename, content)
            with db_for(mode) as db:
                db.execute("UPDATE users SET active=0 WHERE username='candidate'")
            return result
        monkeypatch.setattr(pg_app, 'extract_document_text', revoke)
        assert client.post('/api/applications', params={'job_id':job,'filename':'r.docx'}, content=resume_bytes()).status_code == 401
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM applications').fetchone()['n'] == 0


def test_existing_unowned_schema_is_never_claimed(mode):
    dsn, schema = mode
    with psycopg.connect(dsn, autocommit=True) as db:
        db.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    with pytest.raises(RuntimeError, match='without a valid marker'), TestClient(create_app(seed_demo=True)):
        pass


def test_experimental_mode_requires_demo_and_refuses_sqlite_path(mode):
    with pytest.raises(RuntimeError, match='synthetic demo'):
        create_app(seed_demo=False)
    with pytest.raises(RuntimeError, match='cannot use a SQLite path'):
        create_app(database_path='existing.sqlite3', seed_demo=True)


def test_non_loopback_peer_cannot_spoof_local_host(mode):
    with TestClient(create_app(seed_demo=True), client=('198.51.100.1', 50000)) as client:
        assert client.get('/api/health', headers={'host': 'localhost'}).status_code == 403


def test_session_expiry_during_submission_rolls_back(mode, monkeypatch):
    import time
    from types import SimpleNamespace

    import app.postgres_experimental as pg_app

    with TestClient(create_app(seed_demo=True)) as client:
        login(client, 'candidate')
        job = client.get('/api/jobs').json()[0]['id']
        original = pg_app.submit_application
        def submit_and_expire(*args, **kwargs):
            result = original(*args, **kwargs)
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            return result
        monkeypatch.setattr(pg_app, 'submit_application', submit_and_expire)
        assert client.post('/api/applications', params={'job_id': job, 'filename': 'r.docx'}, content=resume_bytes()).status_code == 401
        with db_for(mode) as db:
            for table in ('applications', 'hr_notifications', 'audit_events', 'provisional_snapshots'):
                assert db.execute(sql.SQL('SELECT count(*) AS n FROM {}').format(sql.Identifier(table))).fetchone()['n'] == 0


def test_unknown_engine_does_not_fall_back_to_sqlite(monkeypatch):
    monkeypatch.setenv('TAWZEEF_DATABASE_ENGINE', 'postgres-experimantal')
    with pytest.raises(RuntimeError, match='Unsupported database engine'):
        create_app(seed_demo=True)

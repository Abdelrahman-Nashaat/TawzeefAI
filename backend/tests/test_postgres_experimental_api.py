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
        assert candidate.get('/api/health').json()['mode'] == 'postgres-experimental-recruitment'
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
        assert other.post('/api/applications/1/decision', json={'decision':'hire'}).status_code == 403
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


def test_hr_screen_is_atomic_and_owner_protected(mode):
    app = create_app(seed_demo=True)
    with TestClient(app) as candidate, TestClient(app) as hr:
        login(candidate, 'candidate')
        job = candidate.get('/api/jobs').json()[0]['id']
        created = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'},
                                 content=resume_bytes())
        assert created.status_code == 201
        application_id = created.json()['id']
        assert candidate.post(f'/api/applications/{application_id}/screen').status_code == 403
        login(hr, 'hr')
        response = hr.post(f'/api/applications/{application_id}/screen')
        assert response.status_code == 200, response.text
        assert 'required' in response.json()
        assert 'score' not in candidate.get('/api/applications').json()[0]
        assert hr.post('/api/applications/999999/screen').status_code == 404
        with db_for(mode) as db:
            row = db.execute('SELECT status,score_json FROM applications WHERE id=%s', (application_id,)).fetchone()
            assert row['status'] == 'submitted' and row['score_json']
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE application_id=%s AND action='screened'", (application_id,)).fetchone()['n'] == 1
            assert db.execute('SELECT count(*) AS n FROM screening_history').fetchone()['n'] == 1


def test_failed_screen_rolls_back_result_and_audit(mode):
    app = create_app(seed_demo=True)
    with TestClient(app) as candidate, TestClient(app) as hr:
        login(candidate, 'candidate')
        job = candidate.get('/api/jobs').json()[0]['id']
        application_id = candidate.post('/api/applications', params={'job_id': job, 'filename':'resume.docx'},
                                        content=resume_bytes()).json()['id']
        with db_for(mode) as db:
            db.execute("ALTER TABLE screening_history ADD CONSTRAINT refuse_test CHECK (score_json = 'never')")
            db.commit()
        login(hr, 'hr')
        # A database failure must abort every change in the screening transaction.
        with pytest.raises(psycopg.errors.CheckViolation):
            hr.post(f'/api/applications/{application_id}/screen')
        with db_for(mode) as db:
            assert db.execute('SELECT score_json FROM applications WHERE id=%s', (application_id,)).fetchone()['score_json'] is None
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='screened'").fetchone()['n'] == 0


def test_expired_session_during_screen_rolls_back(mode, monkeypatch):
    import time

    from app import postgres_experimental

    app = create_app(seed_demo=True)
    with TestClient(app) as candidate, TestClient(app) as hr:
        login(candidate, 'candidate')
        job = candidate.get('/api/jobs').json()[0]['id']
        application_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'},
                                        content=resume_bytes()).json()['id']
        login(hr, 'hr')
        original = postgres_experimental.score_resume
        later = time.time() + 60 * 60 * 24 * 365
        def expire_after_score(job_data, source):
            result = original(job_data, source)
            monkeypatch.setattr(postgres_experimental.time, 'time', lambda: later)
            return result
        monkeypatch.setattr(postgres_experimental, 'score_resume', expire_after_score)
        assert hr.post(f'/api/applications/{application_id}/screen').status_code == 401
        with db_for(mode) as db:
            assert db.execute('SELECT score_json FROM applications WHERE id=%s', (application_id,)).fetchone()['score_json'] is None
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='screened'").fetchone()['n'] == 0
            assert db.execute('SELECT count(*) AS n FROM screening_history').fetchone()['n'] == 0


def test_existing_schema_gains_screening_history_without_losing_data(mode):
    with TestClient(create_app(seed_demo=True)) as candidate:
        login(candidate, 'candidate')
        job = candidate.get('/api/jobs').json()[0]['id']
        application_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'},
                                        content=resume_bytes()).json()['id']
    with db_for(mode) as db:
        db.execute('DROP TABLE screening_history')
    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        assert hr.post(f'/api/applications/{application_id}/screen').status_code == 200
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM screening_history').fetchone()['n'] == 1
            assert db.execute('SELECT count(*) AS n FROM applications').fetchone()['n'] == 1


def test_concurrent_user_revocation_waits_for_screen_transaction(mode, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app import postgres_experimental

    entered, release = Event(), Event()
    original = postgres_experimental.score_resume

    def pause_screen(job_data, source):
        entered.set()
        assert release.wait(10)
        return original(job_data, source)

    monkeypatch.setattr(postgres_experimental, 'score_resume', pause_screen)
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        job = candidate.get('/api/jobs').json()[0]['id']
        application_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'},
                                        content=resume_bytes()).json()['id']
        login(hr, 'hr')
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(hr.post, f'/api/applications/{application_id}/screen')
            try:
                assert entered.wait(10)
                with pytest.raises(psycopg.errors.LockNotAvailable), db_for(mode) as db:
                    db.execute("SET LOCAL lock_timeout = '250ms'")
                    db.execute("UPDATE users SET active=0 WHERE username='hr'")
            finally:
                release.set()
            assert result.result(timeout=10).status_code == 200
        with db_for(mode) as db:
            db.execute("UPDATE users SET active=0 WHERE username='hr'")
        assert hr.post(f'/api/applications/{application_id}/screen').status_code == 401


def pdf_bytes():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    for lines in [('Leave policy', 'Synthetic staff receive vacation days.'),
                  ('Synthetic vacation requests use the portal.',)]:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({
            NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(('BT /F1 12 Tf 50 700 Td ' + ' 0 -18 Td '.join(f'({line}) Tj' for line in lines) + ' ET').encode('ascii'))
        page[NameObject('/Contents')] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_reviewed_pdf_ingestion_provenance_and_access(mode):
    import hashlib
    import json
    content = pdf_bytes()
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        login(hr, 'hr')
        login(candidate, 'candidate')
        assert candidate.post('/api/knowledge/documents/pdf-preview', params={'filename':'policy.pdf'}, content=content).status_code == 403
        from test_partial_pdf_ingestion import make_pdf
        partial = hr.post('/api/knowledge/documents/pdf-preview', params={'filename':'policy.pdf'}, content=make_pdf(['text', 'image']))
        assert partial.status_code == 422 and partial.json()['detail']['code'] == 'partial_text'
        assert hr.post('/api/knowledge/documents/pdf-preview', params={'filename':'policy.pdf'}, content=b'x' * (5 * 1024 * 1024 + 1)).status_code == 413
        preview = hr.post('/api/knowledge/documents/pdf-preview', params={'filename':'policy.pdf'}, content=content)
        assert preview.status_code == 200, preview.text
        assert [p['page'] for p in preview.json()['pages']] == [1, 2]
        params = {'filename':'policy.pdf', 'title':'Synthetic leave', 'audience':'hr', 'collection':'handbook',
                  'digest': preview.json()['digest'], 'marks':json.dumps([{'page':1, 'line':0}])}
        assert candidate.post('/api/knowledge/documents/pdf', params=params, content=content).status_code == 403
        assert hr.post('/api/knowledge/documents/pdf', params={**params, 'digest':'0'*64}, content=content).status_code == 409
        assert hr.post('/api/knowledge/documents/pdf', params=params, content=content + b'\n').status_code == 409
        assert hr.post('/api/knowledge/documents/pdf', params={**params, 'marks':'[{"page":1,"line":999}]'}, content=content).status_code == 422
        response = hr.post('/api/knowledge/documents/pdf', params=params, content=content)
        assert response.status_code == 201, response.text
        assert response.json()['segments'] == 2
        with db_for(mode) as db:
            rows = db.execute('SELECT page,section,body,source_sha256 FROM knowledge_documents ORDER BY id').fetchall()
            assert [r['page'] for r in rows] == [1, 2]
            assert all(r['section'] == 'Leave policy' for r in rows)
            assert [r['body'] for r in rows] == [p['text'] for p in preview.json()['pages']]
            assert all(r['source_sha256'] == hashlib.sha256(content).hexdigest() for r in rows)
            assert db.execute('SELECT count(*) AS n FROM knowledge_audit').fetchone()['n'] == 2
        hr_results = hr.post('/api/knowledge/answer', json={'question':'vacation portal'}).json()['sources']
        assert any(s['page'] == 2 and s['section'] == 'Leave policy' for s in hr_results)
        assert candidate.post('/api/knowledge/answer', json={'question':'vacation portal'}).json()['sources'] == []


def test_reviewed_pdf_atomic_rollback(mode):
    content = pdf_bytes()
    with TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as hr:
        login(hr, 'hr')
        digest = hr.post('/api/knowledge/documents/pdf-preview', params={'filename':'policy.pdf'}, content=content).json()['digest']
        with db_for(mode) as db:
            db.execute('ALTER TABLE knowledge_documents ADD CONSTRAINT reject_second_page CHECK (page <> 2)')
        response = hr.post('/api/knowledge/documents/pdf', params={'filename':'policy.pdf', 'title':'Synthetic',
            'audience':'hr', 'collection':'handbook', 'digest':digest}, content=content)
        assert response.status_code == 500
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM knowledge_documents').fetchone()['n'] == 0
            assert db.execute('SELECT count(*) AS n FROM knowledge_audit').fetchone()['n'] == 0


@pytest.mark.parametrize('reason', ['revoked', 'expired'])
def test_pdf_session_revoked_during_extraction(mode, monkeypatch, reason):
    import app.postgres_knowledge as knowledge
    content = pdf_bytes()
    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        digest = hr.post('/api/knowledge/documents/pdf-preview', params={'filename':'policy.pdf'}, content=content).json()['digest']
        original = knowledge.extract_pdf_pages
        def revoke(filename, data):
            pages = original(filename, data)
            with db_for(mode) as db:
                if reason == 'revoked':
                    db.execute("UPDATE users SET active=0 WHERE username='hr'")
                else:
                    db.execute("UPDATE sessions SET expires_at=0 WHERE user_id=(SELECT id FROM users WHERE username='hr')")
            return pages
        monkeypatch.setattr(knowledge, 'extract_pdf_pages', revoke)
        response = hr.post('/api/knowledge/documents/pdf', params={'filename':'policy.pdf', 'title':'Synthetic',
            'audience':'hr', 'collection':'handbook', 'digest':digest}, content=content)
        assert response.status_code == 401
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM knowledge_documents').fetchone()['n'] == 0
            assert db.execute('SELECT count(*) AS n FROM knowledge_audit').fetchone()['n'] == 0
            db.execute("UPDATE users SET active=1 WHERE username='hr'")
        monkeypatch.setattr(knowledge, 'extract_pdf_pages', original)
        login(hr, 'hr')
        assert hr.post('/api/knowledge/documents/pdf-preview', params={'filename':'policy.pdf'}, content=content).status_code == 200


def test_pdf_processing_saturation_and_slot_cleanup(mode, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import app.postgres_knowledge as knowledge

    content = pdf_bytes()
    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        path = '/api/knowledge/documents/pdf-preview'
        params = {'filename':'policy.pdf'}
        # Failed buffering and extraction must not consume a processing slot.
        assert hr.post(path, params=params, content=b'x' * (5 * 1024 * 1024 + 1)).status_code == 413
        assert hr.post(path, params=params, content=b'not a PDF').status_code == 422
        original = knowledge.extract_pdf_pages
        def fail_extract(filename, data):
            raise RuntimeError('Simulated unexpected extraction failure')
        monkeypatch.setattr(knowledge, 'extract_pdf_pages', fail_extract)
        with pytest.raises(RuntimeError, match='Simulated unexpected extraction failure'):
            hr.post(path, params=params, content=content)
        monkeypatch.setattr(knowledge, 'extract_pdf_pages', original)
        preview = hr.post(path, params=params, content=content)
        assert preview.status_code == 200
        import_params = {'filename':'policy.pdf', 'title':'Synthetic', 'audience':'hr', 'collection':'handbook',
                         'digest':preview.json()['digest']}
        both_entered, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        count = 0

        def slow_extract(filename, data):
            nonlocal count
            with lock:
                count += 1
                if count == 2:
                    both_entered.set()
            if not release.wait(15):
                raise RuntimeError('Timed out waiting for test release')
            return original(filename, data)

        monkeypatch.setattr(knowledge, 'extract_pdf_pages', slow_extract)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(hr.post, path, params=params, content=content)
            second = pool.submit(hr.post, '/api/knowledge/documents/pdf', params=import_params, content=content)
            try:
                assert both_entered.wait(10), 'Two PDF operations did not enter extraction'
                assert hr.post(path, params=params, content=content).status_code == 503
                assert hr.post('/api/knowledge/documents/pdf', params=import_params, content=content).status_code == 503
            finally:
                release.set()
            assert first.result(timeout=15).status_code == 200
            assert second.result(timeout=15).status_code == 201
        monkeypatch.setattr(knowledge, 'extract_pdf_pages', original)
        assert hr.post(path, params=params, content=content).status_code == 200



def test_pdf_cancellation_holds_capacity_until_worker_finishes(mode, monkeypatch):
    import asyncio
    import threading
    from fastapi import HTTPException
    import app.postgres_knowledge as knowledge

    content = pdf_bytes()
    original = knowledge.extract_pdf_pages
    first_entered, both_entered, both_exited, release = (threading.Event() for _ in range(4))
    lock = threading.Lock()
    entered = exited = 0

    class Request:
        async def stream(self):
            yield content

    def slow_extract(filename, data):
        nonlocal entered, exited
        with lock:
            entered += 1
            first_entered.set()
            if entered == 2:
                both_entered.set()
        try:
            if not release.wait(15):
                raise RuntimeError('Timed out waiting for test release')
            return original(filename, data)
        finally:
            with lock:
                exited += 1
                if exited == 2:
                    both_exited.set()

    monkeypatch.setattr(knowledge, 'extract_pdf_pages', slow_extract)

    async def exercise():
        first = asyncio.create_task(knowledge._read_pdf(Request(), 'policy.pdf'))
        second = None
        try:
            assert await asyncio.to_thread(first_entered.wait, 10)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(knowledge._read_pdf(Request(), 'policy.pdf'))
            assert await asyncio.to_thread(both_entered.wait, 10)
            with pytest.raises(HTTPException) as error:
                await knowledge._read_pdf(Request(), 'policy.pdf')
            assert error.value.status_code == 503
        finally:
            release.set()
            if second is not None:
                await asyncio.wait_for(second, timeout=15)
            if both_entered.is_set():
                assert await asyncio.to_thread(both_exited.wait, 10)
        assert (await knowledge._read_pdf(Request(), 'policy.pdf'))[0]
    asyncio.run(exercise())


def test_pdf_slot_cleanup_during_buffer_cancellation_and_submit_failure(mode, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    import app.postgres_knowledge as knowledge
    monkeypatch.setattr(knowledge, '_PDF_EXECUTOR_FAILED', False)
    content = pdf_bytes()

    class Request:
        async def stream(self):
            yield content

    class SlowRequest:
        async def stream(self):
            entered.set()
            await asyncio.Event().wait()
            yield content

    def fail_submit(*args, **kwargs):
        raise RuntimeError('Simulated executor failure')

    async def exercise():
        nonlocal entered
        entered = asyncio.Event()
        task = asyncio.create_task(knowledge._read_pdf(SlowRequest(), 'policy.pdf'))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        original = knowledge._PDF_EXECUTOR.submit
        monkeypatch.setattr(knowledge._PDF_EXECUTOR, 'submit', fail_submit)
        with pytest.raises(HTTPException) as error:
            await knowledge._read_pdf(Request(), 'policy.pdf')
        assert error.value.status_code == 503
        monkeypatch.setattr(knowledge._PDF_EXECUTOR, 'submit', original)
        # Recovery requires a process restart; reset the flag only for this test.
        monkeypatch.setattr(knowledge, '_PDF_EXECUTOR_FAILED', False)
        assert (await knowledge._read_pdf(Request(), 'policy.pdf'))[0]
    entered = None
    asyncio.run(exercise())


def test_pdf_thread_start_failure_never_runs_orphan(mode, monkeypatch):
    import asyncio
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from fastapi import HTTPException
    import app.postgres_knowledge as knowledge

    content = pdf_bytes()
    executor = ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(knowledge, '_PDF_EXECUTOR', executor)
    monkeypatch.setattr(knowledge, '_PDF_EXECUTOR_FAILED', False)
    extracted = []
    monkeypatch.setattr(knowledge, '_extract_pdf_with_digest', lambda *args: extracted.append(args))

    class Request:
        async def stream(self):
            yield content

    def fail_start(self):
        raise RuntimeError('Simulated thread-start failure after queueing')

    async def exercise():
        with monkeypatch.context() as scoped:
            scoped.setattr(threading.Thread, 'start', fail_start)
            with pytest.raises(HTTPException) as error:
                await knowledge._read_pdf(Request(), 'policy.pdf')
            assert error.value.status_code == 503
        assert knowledge._PDF_EXECUTOR_FAILED
        with pytest.raises(HTTPException) as error:
            await knowledge._read_pdf(Request(), 'policy.pdf')
        assert error.value.status_code == 503
        # A later worker drains the queued task; its gate must reject extraction.
        await asyncio.wrap_future(executor.submit(lambda: None))
        assert extracted == []

    try:
        asyncio.run(exercise())
    finally:
        executor.shutdown(wait=True)

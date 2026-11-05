"""Exercise the synthetic scheduling slice against PostgreSQL, including races."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401

from app.main import create_app


def ready_booking(candidate, hr):
    login(candidate, 'candidate')
    login(hr, 'hr')
    job = candidate.get('/api/jobs').json()[0]['id']
    response = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes())
    assert response.status_code == 201, response.text
    application = response.json()['id']
    assert hr.post(f'/api/applications/{application}/screen').status_code == 200
    assert hr.post(f'/api/applications/{application}/decision', json={'decision': 'advance'}).status_code == 200
    start = datetime.now(UTC) + timedelta(days=1)
    response = hr.post('/api/slots', json={'start_at': start.isoformat(), 'end_at': (start + timedelta(hours=1)).isoformat(), 'interviewer': 'Example'})
    assert response.status_code == 201, response.text
    return application, response.json()['id']


def wait_for_lock(mode, locktype):  # noqa: F811
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with db_for(mode) as db:
            waiting = db.execute('''SELECT 1 FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid
                WHERE NOT l.granted AND l.locktype=%s AND a.datname=current_database() LIMIT 1''', (locktype,)).fetchone()
        if waiting:
            return
        time.sleep(0.05)
    raise AssertionError(f'Expected waiting {locktype} lock')


def assert_no_booking(mode):  # noqa: F811
    with db_for(mode) as db:
        for table in ('interviews', 'candidate_interview_bookings'):
            assert db.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n'] == 0
        assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='interview_scheduled'").fetchone()['n'] == 0


def test_booking_expiry_and_audit_failure_roll_back(mode, monkeypatch):  # noqa: F811
    import app.postgres_experimental as pg_app

    with TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application, slot = ready_booking(candidate, hr)
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('tawzeef-scheduling-v1'))")
            future = pool.submit(candidate.post, f'/api/slots/{slot}/book', json={'application_id': application})
            wait_for_lock(mode, 'advisory')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=20).status_code == 401
        assert_no_booking(mode)
        monkeypatch.setattr(pg_app, 'time', time)
        with db_for(mode) as db:
            db.execute("ALTER TABLE audit_events ADD CONSTRAINT reject_booking_audit CHECK (action <> 'interview_scheduled')")
        assert candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).status_code == 500
        assert_no_booking(mode)


def test_booking_denied_if_role_changes_before_authorization(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application, slot = ready_booking(candidate, hr)
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute("UPDATE users SET role='employee' WHERE username='candidate'")
            future = pool.submit(candidate.post, f'/api/slots/{slot}/book', json={'application_id': application})
            wait_for_lock(mode, 'transactionid')
            blocker.commit()
            assert future.result(timeout=20).status_code == 403
        assert_no_booking(mode)


def test_screened_hr_decision_and_concurrent_booking(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    with TestClient(app) as first, TestClient(app) as second, TestClient(app) as hr:
        for client, username in ((first, 'candidate'), (second, 'candidate2'), (hr, 'hr')):
            login(client, username)
        job = first.get('/api/jobs').json()[0]['id']
        ids = []
        for client in (first, second):
            response = client.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes())
            assert response.status_code == 201, response.text
            ids.append(response.json()['id'])
        assert hr.post(f'/api/applications/{ids[0]}/decision', json={'decision': 'advance'}).status_code == 409
        assert first.post(f'/api/applications/{ids[0]}/decision', json={'decision': 'advance'}).status_code == 403
        for app_id in ids:
            response = hr.post(f'/api/applications/{app_id}/screen')
            assert response.status_code == 200, response.text
        for app_id in ids:
            response = hr.post(f'/api/applications/{app_id}/decision', json={'decision': 'advance', 'note': 'Reviewed'})
            assert response.status_code == 200, response.text
        start = datetime.now(UTC) + timedelta(days=1)
        payload = {'start_at': start.isoformat(), 'end_at': (start + timedelta(hours=1)).isoformat(), 'interviewer': 'Example Interviewer'}
        response = hr.post('/api/slots', json=payload)
        assert response.status_code == 201, response.text
        slot = response.json()['id']
        assert hr.post('/api/slots', json=payload).status_code == 409
        assert first.get('/api/slots', params={'application_id': ids[1]}).status_code == 404
        assert len(first.get('/api/slots', params={'application_id': ids[0]}).json()) == 1
        barrier = threading.Barrier(2)
        def book(client, application_id):
            barrier.wait(timeout=10)
            return client.post(f'/api/slots/{slot}/book', json={'application_id': application_id}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(book, first, ids[0])
            b = pool.submit(book, second, ids[1])
            assert sorted([a.result(timeout=20), b.result(timeout=20)]) == [201, 409]
        with db_for(mode) as db:
            assert db.execute("SELECT count(*) AS n FROM interviews WHERE status='scheduled'").fetchone()['n'] == 1
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='decision'").fetchone()['n'] == 2
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='interview_scheduled'").fetchone()['n'] == 1

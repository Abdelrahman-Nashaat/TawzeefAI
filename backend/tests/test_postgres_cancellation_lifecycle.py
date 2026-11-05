"""PostgreSQL cancellation lifecycle and authorization tests on synthetic data."""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401
from test_postgres_scheduling import ready_booking, wait_for_lock

from app.main import create_app


def book(client, application, slot):
    response = client.post(f'/api/slots/{slot}/book', json={'application_id': application})
    assert response.status_code == 201, response.text
    return response.json()['id']


def test_candidate_cancel_and_rebook_persists_with_hr_notice(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as other:
        application, slot = ready_booking(candidate, hr)
        login(other, 'candidate2')
        interview = book(candidate, application, slot)
        assert other.post(f'/api/interviews/{interview}/candidate-cancel').status_code == 404
        assert other.post(f'/api/interviews/{interview}/cancel').status_code == 403
        assert candidate.post(f'/api/interviews/{interview}/candidate-cancel').json()['status'] == 'cancelled'
        assert candidate.post(f'/api/interviews/{interview}/candidate-cancel').status_code == 409
        notices = hr.get('/api/hr/interview-cancellations').json()['items']
        assert len(notices) == 1 and notices[0]['interview_id'] == interview and not notices[0]['is_read']
        assert candidate.get('/api/hr/interview-cancellations').status_code == 403
        assert hr.post(f"/api/hr/interview-cancellations/{notices[0]['id']}/read").status_code == 200
        assert hr.get('/api/hr/interview-cancellations').json()['items'][0]['is_read']
        assert candidate.get('/api/candidate/interview-cancellations').json()['items'] == []
        assert book(candidate, application, slot) != interview
    with TestClient(create_app(seed_demo=True)) as restarted:
        login(restarted, 'hr')
        assert restarted.get('/api/hr/interview-cancellations').json()['items'][0]['is_read']
        assert [i['status'] for i in restarted.get('/api/interviews').json()] == ['cancelled', 'scheduled']
    with db_for(mode) as db:
        assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='interview_cancelled'").fetchone()['n'] == 1


def test_hr_cancel_candidate_notice_is_private_and_readable(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as other:
        application, slot = ready_booking(candidate, hr)
        login(other, 'candidate2')
        interview = book(candidate, application, slot)
        assert hr.post(f'/api/interviews/{interview}/cancel').status_code == 200
        items = candidate.get('/api/candidate/interview-cancellations').json()['items']
        assert len(items) == 1 and items[0]['interview_id'] == interview and not items[0]['is_read']
        notice = items[0]['id']
        assert other.get('/api/candidate/interview-cancellations').json()['items'] == []
        assert other.post(f'/api/candidate/interview-cancellations/{notice}/read').status_code == 404
        assert candidate.post(f'/api/candidate/interview-cancellations/{notice}/read').status_code == 200
        assert candidate.post(f'/api/candidate/interview-cancellations/{notice}/read').status_code == 200
        assert candidate.get('/api/candidate/interview-cancellations').json()['items'][0]['is_read']
        assert hr.get('/api/hr/interview-cancellations').json()['items'] == []
        assert book(candidate, application, slot) != interview


def test_cancel_rollback_and_time_boundary(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as hr:
        application, slot = ready_booking(candidate, hr)
        interview = book(candidate, application, slot)
        with db_for(mode) as db:
            db.execute("ALTER TABLE hr_interview_cancellations ADD CONSTRAINT fail_notice CHECK (id < 0)")
        assert hr.post(f'/api/interviews/{interview}/cancel').status_code == 500
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM interviews WHERE id=%s', (interview,)).fetchone()['status'] == 'scheduled'
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='interview_cancelled'").fetchone()['n'] == 0
            db.execute('ALTER TABLE hr_interview_cancellations DROP CONSTRAINT fail_notice')
            past = datetime.now(UTC) - timedelta(minutes=10)
            db.execute('UPDATE interviews SET start_at=%s,end_at=%s WHERE id=%s', (past, past + timedelta(minutes=5), interview))
        assert hr.post(f'/api/interviews/{interview}/cancel').status_code == 409
        assert candidate.post(f'/api/interviews/{interview}/candidate-cancel').status_code == 409


def test_two_concurrent_cancellations_create_one_notice(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application, slot = ready_booking(candidate, hr)
        interview = book(candidate, application, slot)
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(candidate.post, f'/api/interviews/{interview}/candidate-cancel')
            two = pool.submit(hr.post, f'/api/interviews/{interview}/cancel')
            assert sorted((one.result(timeout=30).status_code, two.result(timeout=30).status_code)) == [200, 409]
        with db_for(mode) as db:
            count = sum(db.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n'] for table in
                        ('candidate_interview_cancellations', 'hr_interview_cancellations'))
            assert count == 1
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='interview_cancelled'").fetchone()['n'] == 1


def test_cancellation_rolls_back_when_session_expires_while_waiting(mode, monkeypatch):  # noqa: F811
    import app.postgres_experimental as pg_app

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application, slot = ready_booking(candidate, hr)
        interview = book(candidate, application, slot)
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('tawzeef-scheduling-v1'))")
            future = pool.submit(candidate.post, f'/api/interviews/{interview}/candidate-cancel')
            wait_for_lock(mode, 'advisory')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=30).status_code == 401
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM interviews WHERE id=%s', (interview,)).fetchone()['status'] == 'scheduled'
            assert db.execute('SELECT count(*) AS n FROM candidate_interview_cancellations').fetchone()['n'] == 0
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='interview_cancelled'").fetchone()['n'] == 0

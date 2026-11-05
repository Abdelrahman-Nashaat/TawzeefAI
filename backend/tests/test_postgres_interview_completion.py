"""The experimental PostgreSQL lifecycle requires explicit HR confirmation."""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_postgres_cancellation_lifecycle import book
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401
from test_postgres_scheduling import ready_booking, wait_for_lock

from app.main import create_app


def end_interview(mode, interview):  # noqa: F811
    past = datetime.now(UTC) - timedelta(hours=2)
    with db_for(mode) as db:
        db.execute('UPDATE interviews SET start_at=%s,end_at=%s WHERE id=%s',
                   (past, past + timedelta(hours=1), interview))


def test_hr_confirms_ended_interview_and_candidate_books_another(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application, slot = ready_booking(candidate, hr)
        interview = book(candidate, application, slot)
        assert candidate.post(f'/api/interviews/{interview}/complete').status_code == 403
        assert hr.post(f'/api/interviews/{interview}/complete').status_code == 409
        end_interview(mode, interview)
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: hr.post(f'/api/interviews/{interview}/complete').status_code, range(2)))
        assert sorted(replies) == [200, 409]
        assert candidate.get('/api/interviews').json()[0]['completed'] is True
        assert hr.get('/api/interviews').json()[0]['completed'] is True
        later = datetime.now(UTC) + timedelta(days=2)
        new_slot = hr.post('/api/slots', json={'start_at': later.isoformat(),
            'end_at': (later + timedelta(hours=1)).isoformat(), 'interviewer': 'Example'})
        assert new_slot.status_code == 201, new_slot.text
        assert new_slot.json()['id'] in [s['id'] for s in candidate.get('/api/slots', params={'application_id': application}).json()]
        assert book(candidate, application, new_slot.json()['id']) != interview
    with TestClient(create_app(seed_demo=True)) as client:
        login(client, 'candidate')
        assert [item['completed'] for item in client.get('/api/interviews').json()] == [True, False]
    with db_for(mode) as db:
        assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='interview_completed'").fetchone()['n'] == 1


def test_completion_rolls_back_if_audit_fails(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as hr:
        application, slot = ready_booking(candidate, hr)
        interview = book(candidate, application, slot)
        end_interview(mode, interview)
        with db_for(mode) as db:
            db.execute("ALTER TABLE audit_events ADD CONSTRAINT reject_completion CHECK (action <> 'interview_completed')")
        assert hr.post(f'/api/interviews/{interview}/complete').status_code == 500
        with db_for(mode) as db:
            assert db.execute('SELECT completed FROM interviews WHERE id=%s', (interview,)).fetchone()['completed'] is False
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='interview_completed'").fetchone()['n'] == 0


def test_completion_rolls_back_when_session_expires_while_waiting(mode, monkeypatch):  # noqa: F811
    import app.postgres_experimental as pg_app

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application, slot = ready_booking(candidate, hr)
        interview = book(candidate, application, slot)
        end_interview(mode, interview)
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('tawzeef-scheduling-v1'))")
            future = pool.submit(hr.post, f'/api/interviews/{interview}/complete')
            wait_for_lock(mode, 'advisory')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=20).status_code == 401
        with db_for(mode) as db:
            assert db.execute('SELECT completed FROM interviews WHERE id=%s', (interview,)).fetchone()['completed'] is False
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='interview_completed'").fetchone()['n'] == 0


def test_existing_experimental_schema_upgrades_without_losing_bookings(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application, slot = ready_booking(candidate, hr)
        interview = book(candidate, application, slot)
    with db_for(mode) as db:
        db.execute('DROP INDEX active_incomplete_interview_application')
        db.execute('ALTER TABLE interviews DROP COLUMN completed')
        db.execute("CREATE UNIQUE INDEX active_interview_application ON interviews(application_id) WHERE status='scheduled'")
    def start_and_read(_):
        with TestClient(create_app(seed_demo=True)) as restarted:
            login(restarted, 'candidate')
            return restarted.get('/api/interviews').json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start_and_read, range(2)))
    for items in results:
        assert items[0]['id'] == interview
        assert items[0]['completed'] is False
    with db_for(mode) as db:
        assert db.execute("SELECT to_regclass('active_interview_application') AS old").fetchone()['old'] is None
        assert db.execute("SELECT to_regclass('active_incomplete_interview_application') AS new").fetchone()['new'] is not None

"""PostgreSQL offer decisions persist, remain private, and require HR approval."""
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_postgres_cancellation_lifecycle import book
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401
from test_postgres_interview_completion import end_interview
from test_postgres_scheduling import ready_booking, wait_for_lock

from app.main import create_app


def complete(mode, candidate, hr):  # noqa: F811
    application, slot = ready_booking(candidate, hr)
    interview = book(candidate, application, slot)
    assert hr.post(f'/api/applications/{application}/offers', json={'details': 'Offer'}).status_code == 409
    end_interview(mode, interview)
    assert hr.post(f'/api/interviews/{interview}/complete').status_code == 200
    return application


def test_offer_acceptance_hire_and_persistence(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as other, TestClient(create_app(seed_demo=True)) as hr:
        application = complete(mode, candidate, hr)
        login(other, 'candidate2')
        endpoint = f'/api/applications/{application}/offers'
        assert candidate.post(endpoint, json={'details': 'Offer'}).status_code == 403
        assert hr.post(endpoint, json={'details': '   '}).status_code == 422
        assert hr.post(f'/api/applications/{application}/decision', json={'decision': 'hire'}).status_code == 409
        response = hr.post(endpoint, json={'details': 'Synthetic terms'})
        assert response.status_code == 201, response.text
        offer = response.json()['id']
        assert other.get(endpoint).status_code == 403
        assert other.post(f'/api/offers/{offer}/respond', json={'decision': 'accepted'}).status_code == 404
        assert candidate.get(endpoint).json()[0]['details'] == 'Synthetic terms'
        assert hr.post(f'/api/applications/{application}/decision', json={'decision': 'hire'}).status_code == 409
        assert candidate.post(f'/api/offers/{offer}/respond', json={'decision': 'accepted'}).status_code == 200
        result = hr.post(f'/api/applications/{application}/decision', json={'decision': 'hire'})
        assert result.status_code == 200, result.text
        assert result.json()['status'] == 'hired'
        assert hr.post(f'/api/applications/{application}/decision', json={'decision': 'hire'}).status_code == 409
    with TestClient(create_app(seed_demo=True)) as restarted:
        login(restarted, 'candidate')
        assert restarted.get(endpoint).json()[0]['status'] == 'accepted'
        assert restarted.get('/api/applications').json()[0]['status'] == 'hired'
    with db_for(mode) as db:
        rows = db.execute("SELECT action,details FROM audit_events WHERE action IN ('offer_presented','offer_accepted')").fetchall()
        assert len(rows) == 2 and all('Synthetic terms' not in row['details'] for row in rows)


def test_acceptance_and_withdrawal_race_is_atomic(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application = complete(mode, candidate, hr)
        offer = hr.post(f'/api/applications/{application}/offers', json={'details': 'Terms'}).json()['id']
        with ThreadPoolExecutor(max_workers=2) as pool:
            accept = pool.submit(candidate.post, f'/api/offers/{offer}/respond', json={'decision': 'accepted'})
            withdraw = pool.submit(hr.post, f'/api/offers/{offer}/withdraw')
            assert sorted([accept.result(timeout=20).status_code, withdraw.result(timeout=20).status_code]) == [200, 409]
        with db_for(mode) as db:
            row = db.execute('SELECT status FROM job_offers WHERE id=%s', (offer,)).fetchone()
            app = db.execute('SELECT status FROM applications WHERE id=%s', (application,)).fetchone()
            assert (row['status'], app['status']) in {('accepted', 'offered'), ('withdrawn', 'advanced')}


def test_decline_and_offer_audit_failure_roll_back(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as hr:
        application = complete(mode, candidate, hr)
        endpoint = f'/api/applications/{application}/offers'
        offer = hr.post(endpoint, json={'details': 'First'}).json()['id']
        assert candidate.post(f'/api/offers/{offer}/respond', json={'decision': 'declined'}).status_code == 200
        with db_for(mode) as db:
            db.execute("ALTER TABLE audit_events ADD CONSTRAINT reject_offer CHECK (action <> 'offer_presented') NOT VALID")
        assert hr.post(endpoint, json={'details': 'Second'}).status_code == 500
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (application,)).fetchone()['status'] == 'advanced'
            assert db.execute('SELECT count(*) AS n FROM job_offers WHERE application_id=%s', (application,)).fetchone()['n'] == 1


def test_offer_session_expiry_while_waiting_rolls_back(mode, monkeypatch):  # noqa: F811
    import app.postgres_experimental as pg_app
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application = complete(mode, candidate, hr)
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('tawzeef-scheduling-v1'))")
            future = pool.submit(hr.post, f'/api/applications/{application}/offers', json={'details': 'Terms'})
            wait_for_lock(mode, 'advisory')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=20).status_code == 401
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM job_offers').fetchone()['n'] == 0
            assert db.execute('SELECT status FROM applications WHERE id=%s', (application,)).fetchone()['status'] == 'advanced'


def test_concurrent_accept_and_hire_never_hires_without_acceptance(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application = complete(mode, candidate, hr)
        offer = hr.post(f'/api/applications/{application}/offers', json={'details': 'Terms'}).json()['id']
        with ThreadPoolExecutor(max_workers=2) as pool:
            acceptance = pool.submit(candidate.post, f'/api/offers/{offer}/respond', json={'decision': 'accepted'})
            hiring = pool.submit(hr.post, f'/api/applications/{application}/decision', json={'decision': 'hire'})
            assert acceptance.result(timeout=20).status_code == 200
            hired = hiring.result(timeout=20).status_code
            assert hired in (200, 409)
        with db_for(mode) as db:
            status = db.execute('SELECT status FROM applications WHERE id=%s', (application,)).fetchone()['status']
            assert status == ('hired' if hired == 200 else 'offered')
            assert db.execute('SELECT status FROM job_offers WHERE id=%s', (offer,)).fetchone()['status'] == 'accepted'
        if hired == 409:
            assert hr.post(f'/api/applications/{application}/decision', json={'decision': 'hire'}).status_code == 200

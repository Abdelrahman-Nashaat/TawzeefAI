"""Rejection reconciles the synthetic PostgreSQL recruitment lifecycle."""
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_postgres_cancellation_lifecycle import book
from test_postgres_experimental_api import db_for, mode  # noqa: F401
from test_postgres_offers import complete
from test_postgres_scheduling import ready_booking, wait_for_lock

from app.main import create_app


def test_reject_cancels_interview_and_notifies_only_owner(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application, slot = ready_booking(candidate, hr)
        interview = book(candidate, application, slot)
        endpoint = f'/api/applications/{application}/decision'
        assert candidate.post(endpoint, json={'decision': 'reject', 'note': 'Reason'}).status_code == 403
        assert hr.post(endpoint, json={'decision': 'reject', 'note': '  '}).status_code == 422
        response = hr.post(endpoint, json={'decision': 'reject', 'note': 'Reason'})
        assert response.status_code == 200 and response.json()['status'] == 'rejected'
        assert hr.post(endpoint, json={'decision': 'reject', 'note': 'Reason'}).status_code == 409
        assert candidate.get('/api/interviews').json()[0]['status'] == 'cancelled'
        notice = candidate.get('/api/candidate/interview-cancellations').json()['items']
        assert len(notice) == 1 and notice[0]['interview_id'] == interview
        assert 'Reason' not in str(notice)
        assert candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).status_code == 409
        with db_for(mode) as db:
            actions = db.execute('SELECT action,details FROM audit_events WHERE application_id=%s', (application,)).fetchall()
            assert sum(a['action'] == 'interview_cancelled' for a in actions) == 1
            assert sum(a['action'] == 'decision' and 'reject' in a['details'] for a in actions) == 1


def test_rejection_notice_failure_rolls_back_everything(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as hr:
        application, slot = ready_booking(candidate, hr)
        interview = book(candidate, application, slot)
        with db_for(mode) as db:
            db.execute('ALTER TABLE hr_interview_cancellations ADD CONSTRAINT no_notices CHECK (id < 0)')
        assert hr.post(f'/api/applications/{application}/decision', json={'decision': 'reject', 'note': 'Reason'}).status_code == 500
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (application,)).fetchone()['status'] == 'advanced'
            assert db.execute('SELECT status FROM interviews WHERE id=%s', (interview,)).fetchone()['status'] == 'scheduled'
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='interview_cancelled'").fetchone()['n'] == 0


def test_rejection_withdraws_presented_offer(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application = complete(mode, candidate, hr)
        offer = hr.post(f'/api/applications/{application}/offers', json={'details': 'Terms'}).json()['id']
        assert hr.post(f'/api/applications/{application}/decision', json={'decision': 'reject', 'note': 'Reason'}).status_code == 200
        assert candidate.post(f'/api/offers/{offer}/respond', json={'decision': 'accepted'}).status_code == 409
        assert candidate.get(f'/api/applications/{application}/offers').json()[0]['status'] == 'withdrawn'


def test_rejection_blocks_accepted_offer(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application = complete(mode, candidate, hr)
        offer = hr.post(f'/api/applications/{application}/offers', json={'details': 'Terms'}).json()['id']
        assert candidate.post(f'/api/offers/{offer}/respond', json={'decision': 'accepted'}).status_code == 200
        assert hr.post(f'/api/applications/{application}/decision', json={'decision': 'reject', 'note': 'Reason'}).status_code == 409
        assert candidate.get(f'/api/applications/{application}/offers').json()[0]['status'] == 'accepted'


def test_rejection_and_offer_acceptance_are_serialized(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application = complete(mode, candidate, hr)
        offer = hr.post(f'/api/applications/{application}/offers', json={'details': 'Terms'}).json()['id']
        with ThreadPoolExecutor(max_workers=2) as pool:
            reject = pool.submit(hr.post, f'/api/applications/{application}/decision', json={'decision': 'reject', 'note': 'Reason'})
            accept = pool.submit(candidate.post, f'/api/offers/{offer}/respond', json={'decision': 'accepted'})
            assert sorted([reject.result(timeout=30).status_code, accept.result(timeout=30).status_code]) == [200, 409]
        with db_for(mode) as db:
            application_status = db.execute('SELECT status FROM applications WHERE id=%s', (application,)).fetchone()['status']
            offer_status = db.execute('SELECT status FROM job_offers WHERE id=%s', (offer,)).fetchone()['status']
            assert (application_status, offer_status) in {('rejected', 'withdrawn'), ('offered', 'accepted')}


def test_rejection_rolls_back_if_session_expires_while_waiting(mode, monkeypatch):  # noqa: F811
    import app.postgres_experimental as pg_app
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application, slot = ready_booking(candidate, hr)
        interview = book(candidate, application, slot)
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('tawzeef-scheduling-v1'))")
            future = pool.submit(hr.post, f'/api/applications/{application}/decision', json={'decision': 'reject', 'note': 'Reason'})
            wait_for_lock(mode, 'advisory')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=30).status_code == 401
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (application,)).fetchone()['status'] == 'advanced'
            assert db.execute('SELECT status FROM interviews WHERE id=%s', (interview,)).fetchone()['status'] == 'scheduled'
            assert db.execute('SELECT count(*) AS n FROM hr_interview_cancellations').fetchone()['n'] == 0

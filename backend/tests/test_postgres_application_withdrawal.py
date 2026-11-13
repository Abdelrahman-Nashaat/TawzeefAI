"""Candidate withdrawal is private, atomic and does not make HR decisions."""
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from test_postgres_cancellation_lifecycle import book
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401
from test_postgres_offers import complete
from test_postgres_scheduling import ready_booking

from app.main import create_app


def test_withdraw_submitted_owner_and_private_hr_notice(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as other, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(other, 'candidate2')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        application = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes()).json()['id']
        endpoint = f'/api/applications/{application}/withdraw'
        assert candidate.post(endpoint, json={'confirmed': False}).status_code == 422
        assert other.post(endpoint, json={'confirmed': True}).status_code == 404
        assert hr.post(endpoint, json={'confirmed': True}).status_code == 403
        assert candidate.post(endpoint, json={'confirmed': True}).json()['status'] == 'withdrawn'
        assert candidate.get(f'/api/workflows/{application}/progress').json()['stage'] == 'withdrawn'
        assert candidate.post(endpoint, json={'confirmed': True}).status_code == 409
        assert candidate.get('/api/hr/application-withdrawals').status_code == 403
        notice = hr.get('/api/hr/application-withdrawals').json()['items'][0]
        assert notice['application_id'] == application and not notice['is_read']
        assert hr.post(f"/api/hr/application-withdrawals/{notice['id']}/read").status_code == 200
        assert hr.get('/api/hr/application-withdrawals').json()['items'][0]['is_read']
        with db_for(mode) as db:
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE application_id=%s AND action='application_withdrawn'", (application,)).fetchone()['n'] == 1
            assert db.execute("SELECT count(*) AS n FROM candidate_application_withdrawals WHERE application_id=%s", (application,)).fetchone()['n'] == 1


def test_withdraw_cancels_future_interview_and_rolls_back_on_audit_failure(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application, slot = ready_booking(candidate, hr)
        interview = book(candidate, application, slot)
        endpoint = f'/api/applications/{application}/withdraw'
        with db_for(mode) as db:
            db.execute("ALTER TABLE candidate_application_withdrawals ADD CONSTRAINT fail_withdraw CHECK (application_id < 0) NOT VALID")
        assert candidate.post(endpoint, json={'confirmed': True}).status_code == 500
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (application,)).fetchone()['status'] == 'advanced'
            assert db.execute('SELECT status FROM interviews WHERE id=%s', (interview,)).fetchone()['status'] == 'scheduled'
            assert db.execute('SELECT count(*) AS n FROM candidate_interview_cancellations').fetchone()['n'] == 0
            db.execute('ALTER TABLE candidate_application_withdrawals DROP CONSTRAINT fail_withdraw')
        assert candidate.post(endpoint, json={'confirmed': True}).status_code == 200
        assert candidate.get(f'/api/interviews/{interview}/calendar').status_code == 409
        notices = hr.get('/api/hr/interview-cancellations').json()['items']
        assert len(notices) == 1 and notices[0]['interview_id'] == interview
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM interviews WHERE id=%s', (interview,)).fetchone()['status'] == 'cancelled'


def test_accepted_offer_blocks_withdrawal_and_accept_race_is_serialized(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application = complete(mode, candidate, hr)
        offer = hr.post(f'/api/applications/{application}/offers', json={'details': 'Synthetic offer'}).json()['id']
        with ThreadPoolExecutor(max_workers=2) as pool:
            withdrawal = pool.submit(candidate.post, f'/api/applications/{application}/withdraw', json={'confirmed': True})
            acceptance = pool.submit(candidate.post, f'/api/offers/{offer}/respond', json={'decision': 'accepted'})
            results = [withdrawal.result(timeout=30).status_code, acceptance.result(timeout=30).status_code]
        assert sorted(results) == [200, 409]
        with db_for(mode) as db:
            app = db.execute('SELECT status FROM applications WHERE id=%s', (application,)).fetchone()['status']
            offer_status = db.execute('SELECT status FROM job_offers WHERE id=%s', (offer,)).fetchone()['status']
            assert (app, offer_status) in {('withdrawn', 'withdrawn'), ('offered', 'accepted')}
        if app == 'offered':
            assert candidate.post(f'/api/applications/{application}/withdraw', json={'confirmed': True}).status_code == 409

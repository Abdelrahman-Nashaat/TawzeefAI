"""Unverified investigation reports never resolve ambiguous email delivery."""
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401
from test_postgres_scheduling import ready_booking, wait_for_lock

from app.main import create_app


def test_investigation_persists_without_resolving_or_revealing_recipient(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def preflight(_kind):
        def dispatch(**kwargs):
            calls.append(kwargs)
            entered.set()
            assert release.wait(20)
            raise TimeoutError('ambiguous')
        return dispatch
    app.state.google_email_preflight = preflight
    with TestClient(app) as candidate, TestClient(app) as hr, ThreadPoolExecutor(max_workers=1) as pool:
        application, slot = ready_booking(candidate, hr)
        interview = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).json()['id']
        endpoint = f'/api/google/interviews/{interview}/email'
        draft = hr.post(endpoint, json={'confirmed': True, 'recipient': 'synthetic@example.com'}).json()
        send = {'confirmed': True, 'delivery_id': draft['id'], 'recipient': draft['recipient'], 'generation': 0}
        future = pool.submit(hr.post, endpoint + '/send', json=send)
        assert entered.wait(20)
        report = {'confirmed': True, 'delivery_id': draft['id'], 'expected_generation': 0,
                  'expected_state': 'sending', 'observation': 'investigating'}
        try:
            assert candidate.get(endpoint + '/investigation').status_code == 403
            assert candidate.post(endpoint + '/investigation', json=report).status_code == 403
            assert hr.post(endpoint + '/investigation', json={**report, 'confirmed': False}).status_code == 422
            assert hr.post(endpoint + '/investigation', json={**report, 'observation': 'proven_sent'}).status_code == 422
            assert hr.post(endpoint + '/investigation', json=report).status_code == 201
            assert candidate.post(f'/api/interviews/{interview}/candidate-cancel').status_code == 409
            assert hr.post(endpoint + '/send', json=send).status_code == 409
        finally:
            release.set()
        assert future.result(timeout=20).json()['state'] == 'uncertain'
        assert hr.post(endpoint + '/investigation', json=report).status_code == 409
        report.update(expected_state='uncertain', observation='reported_message_not_found')
        response = hr.post(endpoint + '/investigation', json=report)
        assert response.status_code == 201
        assert response.json()['state'] == 'uncertain'
        assert 'unverified' in response.json()['notice']
        assert hr.post(endpoint + '/send', json=send).status_code == 409
        assert candidate.post(f'/api/interviews/{interview}/candidate-cancel').status_code == 409
        assert len(calls) == 1
    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        history = hr.get(endpoint + '/investigation').json()
        assert history['state'] == 'uncertain'
        assert [event['observation'] for event in history['events']] == ['reported_message_not_found', 'investigating']
        assert 'synthetic@example.com' not in str(history)


def test_preflight_rejection_serializes_with_investigation(mode, monkeypatch):  # noqa: F811
    """A report cannot be inserted from a state changed during its transaction."""
    import app.postgres_google_email as email

    app = create_app(seed_demo=True)
    app.state.google_email_preflight = lambda _kind: lambda **_kwargs: None
    precheck, proceed = threading.Event(), threading.Event()
    investigation_entered, finish_investigation = threading.Event(), threading.Event()
    original_review, original_audit = email._review, email._audit
    calls = 0

    def review(db, interview_id):
        nonlocal calls
        calls += 1
        # Prepare, claim, then the pre-provider recheck.
        if calls == 3:
            precheck.set()
            assert proceed.wait(20)
            raise HTTPException(status_code=409, detail='Synthetic changed source')
        return original_review(db, interview_id)

    def audit(db, row, actor, action):
        if action == 'google_email_investigation_recorded':
            investigation_entered.set()
            assert finish_investigation.wait(20)
        return original_audit(db, row, actor, action)

    monkeypatch.setattr(email, '_review', review)
    monkeypatch.setattr(email, '_audit', audit)
    with TestClient(app) as candidate, TestClient(app) as hr, TestClient(app) as investigator, ThreadPoolExecutor(max_workers=2) as pool:
        application, slot = ready_booking(candidate, hr)
        login(investigator, 'hr')
        interview = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).json()['id']
        endpoint = f'/api/google/interviews/{interview}/email'
        draft = hr.post(endpoint, json={'confirmed': True, 'recipient': 'synthetic@example.com'}).json()
        send = {'confirmed': True, 'delivery_id': draft['id'], 'recipient': draft['recipient'], 'generation': 0}
        report = {'confirmed': True, 'delivery_id': draft['id'], 'expected_generation': 0,
                  'expected_state': 'sending', 'observation': 'investigating'}
        sending = pool.submit(hr.post, endpoint + '/send', json=send)
        assert precheck.wait(20)
        reporting = pool.submit(investigator.post, endpoint + '/investigation', json=report)
        try:
            wait_for_lock(mode, 'advisory')
            proceed.set()
            assert investigation_entered.wait(20)
            # The rejection must wait until the investigation commits.
            wait_for_lock(mode, 'advisory')
            with db_for(mode) as db:
                assert db.execute('SELECT state FROM pg_email_deliveries').fetchone()['state'] == 'sending'
        finally:
            proceed.set()
            finish_investigation.set()
        assert reporting.result(timeout=20).status_code == 201
        assert sending.result(timeout=20).status_code == 409
        assert hr.get(endpoint).json()['state'] == 'rejected'

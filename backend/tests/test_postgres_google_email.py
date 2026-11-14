"""Only explicitly reviewed, single-attempt synthetic email confirmations are dispatched."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401
from test_postgres_scheduling import ready_booking

from app.main import create_app


def test_reviewed_email_and_role_boundaries(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    calls = []
    def preflight(kind):
        assert kind == 'email'
        def dispatch(**kwargs):
            calls.append(kwargs)
            return {'status': 'confirmed', 'provider_id': 'fake-message'}
        return dispatch
    app.state.google_email_preflight = preflight
    with TestClient(app) as candidate, TestClient(app) as hr:
        application, slot = ready_booking(candidate, hr)
        interview = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).json()['id']
        url = f'/api/google/interviews/{interview}/email'
        assert candidate.post(url, json={'confirmed': True, 'recipient': 'test@example.com'}).status_code == 403
        assert hr.post(url, json={'confirmed': True, 'recipient': 'bad\r\n@example.com'}).status_code == 422
        assert hr.post(url, json={'confirmed': False, 'recipient': 'test@example.com'}).status_code == 422
        draft = hr.post(url, json={'confirmed': True, 'recipient': 'test@example.com'}).json()
        assert draft['state'] == 'pending' and draft['recipient'] == 'test@example.com' and not calls
        assert draft['subject'] == 'Interview confirmation' and draft['body'].startswith('Your interview is scheduled from ')
        send = {'confirmed': True, 'delivery_id': draft['id'], 'recipient': 'wrong@example.com'}
        assert hr.post(url + '/send', json=send).status_code == 409 and not calls
        send['recipient'] = draft['recipient']
        assert hr.post(url + '/send', json=send).json()['state'] == 'confirmed'
        assert len(calls) == 1 and calls[0]['recipient'] == 'test@example.com' and calls[0]['event_id'] is None
        assert hr.post(url + '/send', json=send).status_code == 409
        cancellation = candidate.post(f'/api/interviews/{interview}/candidate-cancel')
        assert cancellation.status_code == 200
        assert cancellation.json()['confirmation_email_sent'] is True
        assert cancellation.json()['cancellation_email_sent'] is False
        assert 'recipient' not in cancellation.json()
    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        assert hr.get(url).json()['state'] == 'confirmed'


def test_uncertain_send_blocks_cancellation_and_never_retries(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    started, release = threading.Event(), threading.Event()
    calls = []
    def preflight(_kind):
        def dispatch(**kwargs):
            calls.append(kwargs)
            started.set()
            assert release.wait(20)
            raise TimeoutError('ambiguous')
        return dispatch
    app.state.google_email_preflight = preflight
    with TestClient(app) as candidate, TestClient(app) as hr, ThreadPoolExecutor(max_workers=1) as pool:
        application, slot = ready_booking(candidate, hr)
        interview = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).json()['id']
        url = f'/api/google/interviews/{interview}/email'
        draft = hr.post(url, json={'confirmed': True, 'recipient': 'test@example.com'}).json()
        payload = {'confirmed': True, 'delivery_id': draft['id'], 'recipient': draft['recipient']}
        future = pool.submit(hr.post, url + '/send', json=payload)
        assert started.wait(20)
        try:
            assert candidate.post(f'/api/interviews/{interview}/candidate-cancel').status_code == 409
            assert candidate.post(f'/api/applications/{application}/withdraw', json={'confirmed': True}).status_code == 409
            assert hr.post(f'/api/applications/{application}/decision', json={'decision': 'reject', 'note': 'reviewed'}).status_code == 409
            assert hr.post(f'/api/interviews/{interview}/cancel').status_code == 409
            assert hr.post(url + '/send', json=payload).status_code == 409
        finally:
            release.set()
        assert future.result(timeout=20).json()['state'] == 'uncertain'
        assert hr.post(url + '/send', json=payload).status_code == 409
        assert len(calls) == 1
        with db_for(mode) as db:
            assert db.execute('SELECT state FROM pg_email_deliveries').fetchone()['state'] == 'uncertain'


def test_session_revoked_during_preflight_does_not_send(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    started, release = threading.Event(), threading.Event()
    calls = []
    def preflight(_kind):
        started.set()
        assert release.wait(20)
        return lambda **kwargs: calls.append(kwargs)
    app.state.google_email_preflight = preflight
    with TestClient(app) as candidate, TestClient(app) as hr, ThreadPoolExecutor(max_workers=1) as pool:
        application, slot = ready_booking(candidate, hr)
        interview = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).json()['id']
        url = f'/api/google/interviews/{interview}/email'
        draft = hr.post(url, json={'confirmed': True, 'recipient': 'test@example.com'}).json()
        payload = {'confirmed': True, 'delivery_id': draft['id'], 'recipient': draft['recipient']}
        future = pool.submit(hr.post, url + '/send', json=payload)
        assert started.wait(20)
        try:
            with db_for(mode) as db:
                db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='hr')")
        finally:
            release.set()
        assert future.result(timeout=20).status_code == 401 and not calls
        with db_for(mode) as db:
            assert db.execute('SELECT state FROM pg_email_deliveries').fetchone()['state'] == 'pending'


def test_disabled_provider_and_changed_template_never_send(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    calls = []
    with TestClient(app) as candidate, TestClient(app) as hr:
        application, slot = ready_booking(candidate, hr)
        interview = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).json()['id']
        url = f'/api/google/interviews/{interview}/email'
        draft = hr.post(url, json={'confirmed': True, 'recipient': 'test@example.com'}).json()
        payload = {'confirmed': True, 'delivery_id': draft['id'], 'recipient': draft['recipient']}
        assert hr.post(url + '/send', json=payload).status_code == 503
        app.state.google_email_preflight = lambda _kind: lambda **kwargs: calls.append(kwargs)
        with db_for(mode) as db:
            db.execute("UPDATE pg_email_deliveries SET body='changed after review'")
        assert hr.post(url + '/send', json=payload).status_code == 409
        assert not calls


def test_changed_context_during_provider_call_is_reported(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    started, release = threading.Event(), threading.Event()
    def preflight(_kind):
        def dispatch(**kwargs):
            started.set()
            assert release.wait(20)
            return {'status': 'confirmed', 'provider_id': 'fake-message'}
        return dispatch
    app.state.google_email_preflight = preflight
    with TestClient(app) as candidate, TestClient(app) as hr, ThreadPoolExecutor(max_workers=1) as pool:
        application, slot = ready_booking(candidate, hr)
        interview = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).json()['id']
        url = f'/api/google/interviews/{interview}/email'
        draft = hr.post(url, json={'confirmed': True, 'recipient': 'test@example.com'}).json()
        payload = {'confirmed': True, 'delivery_id': draft['id'], 'recipient': draft['recipient']}
        future = pool.submit(hr.post, url + '/send', json=payload)
        assert started.wait(20)
        try:
            with db_for(mode) as db:
                db.execute("UPDATE users SET active=0 WHERE username='candidate'")
        finally:
            release.set()
        result = future.result(timeout=20).json()
        assert result['state'] == 'confirmed' and result['context_changed'] is True


@pytest.mark.parametrize('action', ['hr_cancel', 'withdraw', 'reject'])
def test_cancellation_responses_report_prior_confirmation(mode, action):  # noqa: F811
    app = create_app(seed_demo=True)
    app.state.google_email_preflight = lambda kind: lambda **kwargs: {'status': 'confirmed', 'provider_id': 'fake-id'}
    with TestClient(app) as candidate, TestClient(app) as hr:
        application, slot = ready_booking(candidate, hr)
        interview = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).json()['id']
        url = f'/api/google/interviews/{interview}/email'
        draft = hr.post(url, json={'confirmed': True, 'recipient': 'test@example.com'}).json()
        payload = {'confirmed': True, 'delivery_id': draft['id'], 'recipient': draft['recipient']}
        assert hr.post(url + '/send', json=payload).json()['state'] == 'confirmed'
        if action == 'hr_cancel':
            result = hr.post(f'/api/interviews/{interview}/cancel')
        elif action == 'withdraw':
            result = candidate.post(f'/api/applications/{application}/withdraw', json={'confirmed': True})
        else:
            result = hr.post(f'/api/applications/{application}/decision', json={'decision': 'reject', 'note': 'reviewed'})
        assert result.status_code == 200
        assert result.json()['confirmation_email_sent'] is True
        assert result.json()['cancellation_email_sent'] is False
        assert 'recipient' not in result.json()

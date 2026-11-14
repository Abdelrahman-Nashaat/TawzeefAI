"""Unverified investigation reports never resolve ambiguous email delivery."""
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from test_postgres_experimental_api import login, mode  # noqa: F401
from test_postgres_scheduling import ready_booking

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

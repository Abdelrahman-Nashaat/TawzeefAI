"""Recovery never treats missing events as proof of absence or repeats writes."""
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, mode  # noqa: F401
from test_postgres_scheduling import ready_booking

from app.main import create_app

BINDING = 'a' * 64


def configured_app():
    app = create_app(seed_demo=True)
    def preflight(_kind):
        def dispatch(**_kwargs):
            return {'status': 'uncertain'}
        dispatch.google_credential_binding = BINDING
        return dispatch
    app.state.google_preflight = preflight
    return app


def test_recover_existing_exact_event_only_and_preserve_access(mode):  # noqa: F811
    app = configured_app()
    with TestClient(app) as candidate, TestClient(app) as hr:
        application, slot = ready_booking(candidate, hr)
        interview = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).json()['id']
        url = f'/api/google/interviews/{interview}/calendar'
        draft = hr.post(url, json={'confirmed': True}).json()
        payload = {'confirmed': True, 'event_id': draft['event_id']}
        assert hr.post(url + '/reconcile', json=payload).status_code == 409
        assert hr.post(url + '/send', json=payload).json()['state'] == 'uncertain'
        calls = []
        def preflight():
            def inspect(**kwargs):
                calls.append(kwargs)
                return {'status': 'uncertain'}
            inspect.google_credential_binding = BINDING
            return inspect
        app.state.google_calendar_inspection_preflight = preflight
        assert candidate.post(url + '/reconcile', json=payload).status_code == 403
        assert hr.post(url + '/reconcile', json=payload).json()['state'] == 'uncertain'
        assert candidate.post(f'/api/interviews/{interview}/candidate-cancel').status_code == 409
        def wrong_binding():
            def inspect(**_kwargs):
                raise AssertionError('Inspection must not use another credential')
            inspect.google_credential_binding = 'b' * 64
            return inspect
        app.state.google_calendar_inspection_preflight = wrong_binding
        assert hr.post(url + '/reconcile', json=payload).status_code == 503
        def found():
            def inspect(**kwargs):
                return {'status': 'confirmed', 'event_id': kwargs['event_id'], 'etag': '"v1"'}
            inspect.google_credential_binding = BINDING
            return inspect
        app.state.google_calendar_inspection_preflight = found
        result = hr.post(url + '/reconcile', json=payload)
        assert result.json()['state'] == 'confirmed'
        assert hr.post(url + '/reconcile', json=payload).status_code == 409
        assert len(calls) == 1
        def remove_preflight():
            def remove(**_kwargs):
                return {'status': 'uncertain'}
            remove.google_credential_binding = BINDING
            return remove
        app.state.google_calendar_removal_preflight = remove_preflight
        assert hr.post(url + '/remove', json=payload).json()['state'] == 'removal_uncertain'
        def changed_version():
            def inspect(**kwargs):
                return {'status': 'confirmed', 'event_id': kwargs['event_id'], 'etag': '"v2"'}
            inspect.google_credential_binding = BINDING
            return inspect
        app.state.google_calendar_inspection_preflight = changed_version
        assert hr.post(url + '/reconcile', json=payload).json()['state'] == 'removal_uncertain'
        app.state.google_calendar_inspection_preflight = found
        assert hr.post(url + '/reconcile', json=payload).json()['state'] == 'confirmed'


def test_session_revocation_during_inspection_does_not_update(mode):  # noqa: F811
    app = configured_app()
    started, release = threading.Event(), threading.Event()
    def preflight():
        def inspect(**kwargs):
            started.set()
            assert release.wait(30)
            return {'status': 'confirmed', 'event_id': kwargs['event_id'], 'etag': '"v1"'}
        inspect.google_credential_binding = BINDING
        return inspect
    app.state.google_calendar_inspection_preflight = preflight
    with TestClient(app) as candidate, TestClient(app) as hr, ThreadPoolExecutor(max_workers=1) as pool:
        application, slot = ready_booking(candidate, hr)
        interview = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).json()['id']
        url = f'/api/google/interviews/{interview}/calendar'
        draft = hr.post(url, json={'confirmed': True}).json()
        payload = {'confirmed': True, 'event_id': draft['event_id']}
        assert hr.post(url + '/send', json=payload).json()['state'] == 'uncertain'
        future = pool.submit(hr.post, url + '/reconcile', json=payload)
        assert started.wait(30)
        try:
            with db_for(mode) as db:
                db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='hr')")
        finally:
            release.set()
        assert future.result(timeout=30).status_code == 401
        with db_for(mode) as db:
            assert db.execute('SELECT state FROM pg_calendar_deliveries').fetchone()['state'] == 'uncertain'

"""Google Calendar actions use a durable one-attempt claim and explicit review."""
import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, mode  # noqa: F401
from test_postgres_scheduling import ready_booking

from app.main import create_app

BINDING = 'a' * 64


def test_calendar_confirmation_removal_and_roles(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    calls = []
    def preflight(kind):
        assert kind == 'calendar'
        def dispatch(**kwargs):
            calls.append(kwargs)
            return {'status': 'confirmed', 'provider_id': kwargs['event_id'], 'etag': '"v1"'}
        dispatch.google_credential_binding = BINDING
        return dispatch
    def preflight_remove():
        def remove(**kwargs):
            calls.append(kwargs)
            return {'status': 'removed'}
        remove.google_credential_binding = BINDING
        return remove
    app.state.google_preflight = preflight
    app.state.google_calendar_removal_preflight = preflight_remove
    with TestClient(app) as candidate, TestClient(app) as hr:
        application, slot = ready_booking(candidate, hr)
        interview = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).json()['id']
        url = f'/api/google/interviews/{interview}/calendar'
        assert candidate.post(url, json={'confirmed': True}).status_code == 403
        assert hr.post(url, json={'confirmed': False}).status_code == 422
        draft = hr.post(url, json={'confirmed': True}).json()
        assert draft['state'] == 'pending' and not calls
        assert hr.post(url + '/send', json={'confirmed': True, 'event_id': 'wrong'}).status_code == 409
        payload = {'confirmed': True, 'event_id': draft['event_id']}
        assert hr.post(url + '/send', json=payload).json()['state'] == 'confirmed'
        assert len(calls) == 1 and calls[0]['recipient'] is None
        assert hr.post(url + '/send', json=payload).status_code == 409
        assert candidate.post(f'/api/interviews/{interview}/candidate-cancel').status_code == 409
        assert candidate.post(f'/api/applications/{application}/withdraw', json={'confirmed': True}).status_code == 409
        assert hr.post(f'/api/applications/{application}/decision', json={'decision': 'reject', 'note': 'reviewed'}).status_code == 409
        assert hr.post(f'/api/interviews/{interview}/cancel').status_code == 409
        def wrong_binding():
            def remove(**kwargs):
                raise AssertionError('Removal must not run with a different credential')
            remove.google_credential_binding = 'b' * 64
            return remove
        app.state.google_calendar_removal_preflight = wrong_binding
        assert hr.post(url + '/remove', json=payload).status_code == 503
        app.state.google_calendar_removal_preflight = preflight_remove
        assert hr.post(url + '/remove', json=payload).json()['state'] == 'removed'
        assert hr.post(url + '/remove', json=payload).status_code == 409
        assert candidate.post(f'/api/interviews/{interview}/candidate-cancel').status_code == 200
    with TestClient(create_app(seed_demo=True)) as hr:
        from test_postgres_experimental_api import login
        login(hr, 'hr')
        assert hr.get(url).json()['state'] == 'removed'


def test_inflight_blocks_cancellation_and_ambiguous_outcome_never_retries(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    started, release = threading.Event(), threading.Event()
    calls = []
    def preflight(_kind):
        def dispatch(**kwargs):
            calls.append(kwargs)
            started.set()
            assert release.wait(20)
            raise TimeoutError('ambiguous')
        dispatch.google_credential_binding = BINDING
        return dispatch
    app.state.google_preflight = preflight
    with TestClient(app) as candidate, TestClient(app) as hr, ThreadPoolExecutor(max_workers=1) as pool:
        application, slot = ready_booking(candidate, hr)
        interview = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).json()['id']
        url = f'/api/google/interviews/{interview}/calendar'
        draft = hr.post(url, json={'confirmed': True}).json()
        payload = {'confirmed': True, 'event_id': draft['event_id']}
        future = pool.submit(hr.post, url + '/send', json=payload)
        assert started.wait(20)
        try:
            assert candidate.post(f'/api/interviews/{interview}/candidate-cancel').status_code == 409
            assert hr.post(url + '/send', json=payload).status_code == 409
        finally:
            release.set()
        assert future.result(timeout=20).json()['state'] == 'uncertain'
        assert hr.post(url + '/send', json=payload).status_code == 409
        assert len(calls) == 1
        with db_for(mode) as db:
            assert db.execute('SELECT state FROM pg_calendar_deliveries').fetchone()['state'] == 'uncertain'


def test_changed_context_during_send_is_reported_and_does_not_rewrite_outcome(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    started, release = threading.Event(), threading.Event()
    def preflight(_kind):
        def dispatch(**kwargs):
            started.set()
            assert release.wait(20)
            return {'status': 'confirmed', 'provider_id': kwargs['event_id'], 'etag': '"v1"'}
        dispatch.google_credential_binding = BINDING
        return dispatch
    app.state.google_preflight = preflight
    with TestClient(app) as candidate, TestClient(app) as hr, ThreadPoolExecutor(max_workers=1) as pool:
        application, slot = ready_booking(candidate, hr)
        interview = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).json()['id']
        url = f'/api/google/interviews/{interview}/calendar'
        draft = hr.post(url, json={'confirmed': True}).json()
        future = pool.submit(hr.post, url + '/send', json={'confirmed': True, 'event_id': draft['event_id']})
        assert started.wait(20)
        try:
            with db_for(mode) as db:
                db.execute("UPDATE users SET active=0 WHERE username='candidate'")
        finally:
            release.set()
        result = future.result(timeout=20).json()
        assert result['state'] == 'confirmed' and result['context_changed'] is True


def test_expired_session_during_preflight_never_contacts_provider(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    started, release = threading.Event(), threading.Event()
    calls = []
    def preflight(_kind):
        started.set()
        assert release.wait(20)
        def dispatch(**kwargs):
            calls.append(kwargs)
        dispatch.google_credential_binding = BINDING
        return dispatch
    app.state.google_preflight = preflight
    with TestClient(app) as candidate, TestClient(app) as hr, ThreadPoolExecutor(max_workers=1) as pool:
        application, slot = ready_booking(candidate, hr)
        interview = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).json()['id']
        url = f'/api/google/interviews/{interview}/calendar'
        draft = hr.post(url, json={'confirmed': True}).json()
        future = pool.submit(hr.post, url + '/send', json={'confirmed': True, 'event_id': draft['event_id']})
        assert started.wait(20)
        try:
            with db_for(mode) as db:
                db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='hr')")
        finally:
            release.set()
        assert future.result(timeout=20).status_code == 401
        assert not calls
        with db_for(mode) as db:
            assert db.execute('SELECT state FROM pg_calendar_deliveries').fetchone()['state'] == 'pending'


def test_expired_session_after_removal_claim_does_not_call_google(mode, monkeypatch):  # noqa: F811
    import time
    from types import SimpleNamespace

    import app.postgres_experimental as pg_app

    app = create_app(seed_demo=True)
    calls = []
    def preflight(_kind):
        def dispatch(**kwargs):
            return {'status': 'confirmed', 'provider_id': kwargs['event_id'], 'etag': '"v1"'}
        dispatch.google_credential_binding = BINDING
        return dispatch
    def removal_preflight():
        def remove(**kwargs):
            calls.append(kwargs)
            return {'status': 'removed'}
        remove.google_credential_binding = BINDING
        return remove
    app.state.google_preflight = preflight
    app.state.google_calendar_removal_preflight = removal_preflight
    with TestClient(app) as candidate, TestClient(app) as hr:
        application, slot = ready_booking(candidate, hr)
        interview = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).json()['id']
        url = f'/api/google/interviews/{interview}/calendar'
        draft = hr.post(url, json={'confirmed': True}).json()
        payload = {'confirmed': True, 'event_id': draft['event_id']}
        assert hr.post(url + '/send', json=payload).json()['state'] == 'confirmed'
        def clock():
            with db_for(mode) as db:
                current = db.execute('SELECT state FROM pg_calendar_deliveries').fetchone()['state']
            return time.time() + (365 * 86400 if current == 'removing' else 0)
        monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=clock))
        assert hr.post(url + '/remove', json=payload).status_code == 401
        assert not calls
        with db_for(mode) as db:
            assert db.execute('SELECT state FROM pg_calendar_deliveries').fetchone()['state'] == 'confirmed'


def test_concurrent_removal_is_attempted_only_once(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    started, release = threading.Event(), threading.Event()
    calls = []
    def preflight(_kind):
        def dispatch(**kwargs):
            return {'status': 'confirmed', 'provider_id': kwargs['event_id'], 'etag': '"v1"'}
        dispatch.google_credential_binding = BINDING
        return dispatch
    def removal_preflight():
        def remove(**kwargs):
            calls.append(kwargs)
            started.set()
            assert release.wait(20)
            return {'status': 'removed'}
        remove.google_credential_binding = BINDING
        return remove
    app.state.google_preflight = preflight
    app.state.google_calendar_removal_preflight = removal_preflight
    with TestClient(app) as candidate, TestClient(app) as hr, ThreadPoolExecutor(max_workers=1) as pool:
        application, slot = ready_booking(candidate, hr)
        interview = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).json()['id']
        url = f'/api/google/interviews/{interview}/calendar'
        draft = hr.post(url, json={'confirmed': True}).json()
        payload = {'confirmed': True, 'event_id': draft['event_id']}
        assert hr.post(url + '/send', json=payload).json()['state'] == 'confirmed'
        future = pool.submit(hr.post, url + '/remove', json=payload)
        assert started.wait(20)
        try:
            assert hr.post(url + '/remove', json=payload).status_code == 409
            assert candidate.post(f'/api/interviews/{interview}/candidate-cancel').status_code == 409
        finally:
            release.set()
        assert future.result(timeout=20).json()['state'] == 'removed'
        assert len(calls) == 1


def test_review_user_lock_is_compatible_with_waiting_candidate_cancel(mode):  # noqa: F811
    from test_postgres_scheduling import wait_for_lock

    from app.postgres_google_calendar import _review

    app = create_app(seed_demo=True)
    with TestClient(app) as candidate, TestClient(app) as hr, ThreadPoolExecutor(max_workers=1) as pool:
        application, slot = ready_booking(candidate, hr)
        interview = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).json()['id']
        with db_for(mode) as holder:
            holder.execute("SET LOCAL statement_timeout = '2s'")
            holder.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('tawzeef-scheduling-v1'))")
            future = pool.submit(candidate.post, f'/api/interviews/{interview}/candidate-cancel')
            wait_for_lock(mode, 'advisory')
            assert _review(holder, interview)['id'] == interview
            holder.commit()
            assert future.result(timeout=20).status_code == 200

import importlib
import importlib.util
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.fixture
def demo(tmp_path):
    assert importlib.util.find_spec('app.google_outbox') is not None
    outbox = importlib.import_module('app.google_outbox')
    path = tmp_path / 'outbox.sqlite3'
    app = create_app(path, seed_demo=True)
    app.include_router(outbox.router)
    with TestClient(app) as client:
        outbox.initialize_google_outbox(path)
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            app_id = db.execute('''INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?, 'job-backend-python', 'synthetic.docx', 'synthetic')''', (candidate,)).lastrowid
            start = (datetime.now(UTC) + timedelta(days=2)).isoformat()
            end = (datetime.now(UTC) + timedelta(days=2, hours=1)).isoformat()
            interview = db.execute('''INSERT INTO interviews(application_id,candidate_id,start_at,end_at,
                interviewer,interviewer_key,created_by) VALUES (?,?,?,?,?,?,?)''',
                (app_id, candidate, start, end, 'synthetic', 'synthetic', hr)).lastrowid
        yield client, app, path, interview, hr


def login(client, username):
    assert client.post('/api/auth/login', json={'username': username, 'password': f'{username}-demo-only'}).status_code == 200


def test_drafts_require_hr_and_confirmation_and_never_send_by_default(demo):
    client, _, path, interview, _ = demo
    endpoint = f'/api/google/interviews/{interview}/deliveries'
    assert client.get(endpoint).status_code == 401
    login(client, 'candidate')
    assert client.get(endpoint).status_code == 403
    assert client.post(endpoint, json={'kind': 'calendar', 'confirmed': True}).status_code == 403
    login(client, 'hr')
    assert client.post(endpoint, json={'kind': 'email', 'recipient': 'candidate@example.test'}).status_code == 422
    assert client.post(endpoint, json={'kind': 'calendar', 'recipient': 'candidate@example.test', 'confirmed': True}).status_code == 422
    created = client.post(endpoint, json={'kind': 'calendar', 'confirmed': True})
    assert created.status_code == 201
    delivery = created.json()
    assert delivery['state'] == 'pending' and delivery['recipient'] is None
    assert len(delivery['event_id']) == 32
    same = client.post(endpoint, json={'kind': 'calendar', 'confirmed': True}).json()
    assert same['id'] == delivery['id'] and same['event_id'] == delivery['event_id']
    assert client.post(f"/api/google/deliveries/{delivery['id']}/send", json={'confirmed': True}).status_code == 503
    with connect(path) as db:
        assert db.execute('SELECT state FROM google_deliveries WHERE id=?', (delivery['id'],)).fetchone()[0] == 'pending'


def test_missing_provider_credentials_leave_draft_pending(demo):
    client, app, path, interview, _ = demo
    login(client, 'hr')
    delivery = client.post(f'/api/google/interviews/{interview}/deliveries', json={
        'kind': 'calendar', 'confirmed': True,
    }).json()
    calls = []
    app.state.google_dispatcher = lambda **kwargs: calls.append(kwargs)

    def unavailable(kind):
        raise RuntimeError('sensitive credential details')

    app.state.google_preflight = unavailable
    response = client.post(f"/api/google/deliveries/{delivery['id']}/send", json={'confirmed': True})
    assert response.status_code == 503 and 'sensitive credential details' not in response.text
    assert not calls
    with connect(path) as db:
        assert db.execute('SELECT state FROM google_deliveries WHERE id=?', (delivery['id'],)).fetchone()[0] == 'pending'


def test_preflight_prepares_request_local_dispatch_and_rechecks_interview(demo):
    client, app, path, interview, _ = demo
    login(client, 'hr')
    delivery = client.post(f'/api/google/interviews/{interview}/deliveries', json={
        'kind': 'calendar', 'confirmed': True,
    }).json()
    calls = []
    app.state.google_dispatcher = lambda **kwargs: pytest.fail('global dispatcher should not be used')

    def preflight(kind):
        assert kind == 'calendar'
        with connect(path) as db:
            db.execute("UPDATE interviews SET status='cancelled' WHERE id=?", (interview,))
        return lambda **kwargs: calls.append(kwargs)

    app.state.google_preflight = preflight
    response = client.post(f"/api/google/deliveries/{delivery['id']}/send", json={'confirmed': True})
    assert response.status_code == 409
    assert calls == []
    with connect(path) as db:
        assert db.execute('SELECT state FROM google_deliveries WHERE id=?', (delivery['id'],)).fetchone()[0] == 'pending'


def test_preflight_request_local_dispatcher_is_used(demo):
    client, app, _, interview, _ = demo
    login(client, 'hr')
    delivery = client.post(f'/api/google/interviews/{interview}/deliveries', json={
        'kind': 'calendar', 'confirmed': True,
    }).json()
    app.state.google_dispatcher = lambda **kwargs: pytest.fail('global dispatcher should not be used')
    app.state.google_preflight = lambda kind: lambda **kwargs: {
        'status': 'confirmed', 'provider_id': kwargs['event_id'],
    }
    response = client.post(f"/api/google/deliveries/{delivery['id']}/send", json={'confirmed': True})
    assert response.status_code == 200 and response.json()['state'] == 'confirmed'


def test_timeout_and_crash_state_prevent_duplicate_send(demo):
    client, app, path, interview, _ = demo
    login(client, 'hr')
    endpoint = f'/api/google/interviews/{interview}/deliveries'
    delivery = client.post(endpoint, json={'kind': 'email', 'recipient': 'candidate@example.test', 'confirmed': True}).json()
    entered, release = threading.Event(), threading.Event()
    calls = []

    def dispatch(**kwargs):
        calls.append(kwargs)
        entered.set()
        assert release.wait(timeout=10)
        raise TimeoutError('private provider details')

    app.state.google_dispatcher = dispatch
    target = f"/api/google/deliveries/{delivery['id']}/send"
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, target, json={'confirmed': True})
        try:
            assert entered.wait(timeout=10)
            assert client.post(target, json={'confirmed': True}).status_code == 409
        finally:
            release.set()
        result = first.result(timeout=10)
    assert result.status_code == 200 and result.json()['state'] == 'uncertain'
    assert 'private provider details' not in result.text and len(calls) == 1
    assert client.post(target, json={'confirmed': True}).status_code == 409
    with connect(path) as db:
        audit = str([dict(row) for row in db.execute("SELECT * FROM audit_events WHERE action LIKE 'google_%'")])
    assert 'candidate@example.test' not in audit


def test_inactive_account_and_cancelled_interview_cannot_be_dispatched(demo):
    client, app, path, interview, hr = demo
    login(client, 'hr')
    endpoint = f'/api/google/interviews/{interview}/deliveries'
    delivery = client.post(endpoint, json={'kind': 'calendar', 'confirmed': True}).json()
    calls = []
    app.state.google_dispatcher = lambda **kwargs: calls.append(kwargs)
    with connect(path) as db:
        db.execute('UPDATE users SET active=0 WHERE id=?', (hr,))
    assert client.post(f"/api/google/deliveries/{delivery['id']}/send", json={'confirmed': True}).status_code == 401
    with connect(path) as db:
        db.execute('UPDATE users SET active=1 WHERE id=?', (hr,))
        db.execute("UPDATE interviews SET status='cancelled' WHERE id=?", (interview,))
    assert client.post(f"/api/google/deliveries/{delivery['id']}/send", json={'confirmed': True}).status_code == 409
    assert not calls


def test_revocation_during_dispatch_hides_result_but_records_outcome(demo):
    client, app, path, interview, hr = demo
    login(client, 'hr')
    delivery = client.post(f'/api/google/interviews/{interview}/deliveries', json={
        'kind': 'email', 'recipient': 'candidate@example.test', 'confirmed': True,
    }).json()

    def dispatch(**kwargs):
        with connect(path) as db:
            db.execute('UPDATE users SET active=0 WHERE id=?', (hr,))
        return {'status': 'confirmed', 'provider_id': 'message-1'}

    app.state.google_dispatcher = dispatch
    result = client.post(f"/api/google/deliveries/{delivery['id']}/send", json={'confirmed': True})
    assert result.status_code == 401 and 'candidate@example.test' not in result.text
    with connect(path) as db:
        assert db.execute('SELECT state FROM google_deliveries WHERE id=?', (delivery['id'],)).fetchone()[0] == 'confirmed'


def test_sending_survives_restart_and_blocks_retries_and_cancellation(demo):
    client, _, path, interview, _ = demo
    module = importlib.import_module('app.google_outbox')
    login(client, 'hr')
    delivery = client.post(f'/api/google/interviews/{interview}/deliveries', json={
        'kind': 'calendar', 'confirmed': True,
    }).json()
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        assert module.cancellation_block_reason(db, interview) is None
        db.execute("UPDATE google_deliveries SET state='sending' WHERE id=?", (delivery['id'],))
        assert module.cancellation_block_reason(db, interview) == 'google_delivery_in_flight_or_unresolved'
    restarted = create_app(path, seed_demo=True)
    restarted.include_router(module.router)
    calls = []
    restarted.state.google_dispatcher = lambda **kwargs: calls.append(kwargs)
    with TestClient(restarted) as again:
        module.initialize_google_outbox(path)
        login(again, 'hr')
        assert again.get(f'/api/google/interviews/{interview}/deliveries').json()[0]['state'] == 'sending'
        assert again.post(f"/api/google/deliveries/{delivery['id']}/send", json={'confirmed': True}).status_code == 409
    assert calls == []
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE google_deliveries SET state='confirmed' WHERE id=?", (delivery['id'],))
        assert module.cancellation_block_reason(db, interview) == 'google_calendar_requires_reconciliation'


def test_actor_disabled_after_authentication_is_rechecked_in_transaction(demo):
    from app.auth import User, get_current_user

    client, app, path, interview, hr = demo
    login(client, 'hr')

    def stale_identity():
        with connect(path) as db:
            row = db.execute('SELECT username,display_name FROM users WHERE id=?', (hr,)).fetchone()
            db.execute('UPDATE users SET active=0 WHERE id=?', (hr,))
        return User(id=hr, username=row['username'], display_name=row['display_name'], role='hr')

    app.dependency_overrides[get_current_user] = stale_identity
    try:
        response = client.post(f'/api/google/interviews/{interview}/deliveries', json={
            'kind': 'calendar', 'confirmed': True,
        })
        assert response.status_code == 401
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM google_deliveries').fetchone()[0] == 0
    finally:
        app.dependency_overrides.clear()

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def bound(callback, binding='a' * 64):
    callback.google_credential_binding = binding
    return callback


@pytest.fixture
def confirmed(tmp_path):
    path = tmp_path / 'reconciliation.sqlite3'
    app = create_app(path, seed_demo=True)
    app.state.google_dispatcher = bound(lambda **kw: {
        'status': 'confirmed', 'provider_id': kw['event_id'], 'etag': '"original"',
    })
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?, 'job-backend-python', 'synthetic.docx', 'synthetic')""", (candidate,)).lastrowid
        start = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
        interview = client.post('/api/interviews', json={'application_id': application, 'start_at': start.isoformat(),
            'end_at': (start + timedelta(hours=1)).isoformat(), 'interviewer': 'Synthetic HR'}).json()['id']
        delivery = client.post(f'/api/google/interviews/{interview}/deliveries',
            json={'kind': 'calendar', 'confirmed': True}).json()['id']
        assert client.post(f'/api/google/deliveries/{delivery}/send', json={'confirmed': True}).json()['state'] == 'confirmed'
        yield client, app, path, interview, delivery


def install_remover(app, callback, binding='a' * 64):
    app.state.google_calendar_removal_preflight = lambda: bound(callback, binding)


def test_reconcile_requires_same_credential_and_allows_cancel_only_after_removed(confirmed):
    client, app, path, interview, delivery = confirmed
    calls = []
    install_remover(app, lambda **kw: calls.append(kw) or {'status': 'removed'}, 'b' * 64)
    url = f'/api/google/deliveries/{delivery}/calendar-removal'
    assert client.post(url, json={'confirmed': True}).status_code == 409
    assert client.post(f'/api/interviews/{interview}/cancel').status_code == 409
    assert calls == []
    install_remover(app, lambda **kw: calls.append(kw) or {'status': 'removed'})
    assert client.post(url, json={'confirmed': False}).status_code == 422
    response = client.post(url, json={'confirmed': True})
    assert response.status_code == 200 and response.json()['state'] == 'removed'
    assert len(calls) == 1 and calls[0]['etag'] == '"original"'
    assert client.post(url, json={'confirmed': True}).status_code == 409
    assert client.post(f'/api/interviews/{interview}/cancel').status_code == 200
    with connect(path) as db:
        assert db.execute('SELECT state FROM google_calendar_removals').fetchone()[0] == 'removed'


def test_reconcile_claim_blocks_cancellation_and_duplicate_even_if_process_fails(confirmed):
    client, app, path, interview, delivery = confirmed
    entered, release = threading.Event(), threading.Event()
    calls = []
    def remove(**kw):
        calls.append(kw)
        entered.set()
        assert release.wait(timeout=10)
        raise TimeoutError('private data')
    install_remover(app, remove)
    url = f'/api/google/deliveries/{delivery}/calendar-removal'
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, url, json={'confirmed': True})
        try:
            assert entered.wait(timeout=10)
            assert client.post(url, json={'confirmed': True}).status_code == 409
            assert client.post(f'/api/interviews/{interview}/cancel').status_code == 409
        finally:
            release.set()
        result = first.result(timeout=10)
    assert result.status_code == 200 and result.json()['state'] == 'uncertain'
    assert 'private data' not in result.text and len(calls) == 1
    assert client.post(url, json={'confirmed': True}).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT state FROM google_calendar_removals').fetchone()[0] == 'uncertain'


def test_calendar_removal_and_email_delivery_are_serialized(confirmed):
    client, app, _, interview, delivery = confirmed
    email = client.post(f'/api/google/interviews/{interview}/deliveries', json={
        'kind': 'email', 'recipient': 'candidate@example.test', 'confirmed': True,
    }).json()['id']
    entered, release = threading.Event(), threading.Event()
    calls = []
    def dispatch(**kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return {'status': 'confirmed', 'provider_id': 'fake-message'}
    app.state.google_dispatcher = dispatch
    install_remover(app, lambda **kw: calls.append(kw) or {'status': 'removed'})
    url = f'/api/google/deliveries/{delivery}/calendar-removal'
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, f'/api/google/deliveries/{email}/send', json={'confirmed': True, 'expected_recipient': 'candidate@example.test'})
        try:
            assert entered.wait(timeout=10)
            assert client.post(url, json={'confirmed': True}).status_code == 409
            assert calls == []
        finally:
            release.set()
        assert pending.result(timeout=10).json()['state'] == 'confirmed'
    assert client.post(url, json={'confirmed': True}).json()['state'] == 'removed'
    items = client.get(f'/api/google/interviews/{interview}/deliveries').json()
    assert next(x for x in items if x['kind'] == 'calendar')['calendar_removal_state'] == 'removed'


@pytest.mark.parametrize('removal_state', ['removing', 'removed', 'uncertain', 'rejected'])
def test_email_cannot_send_after_calendar_removal_starts(confirmed, removal_state):
    client, app, path, interview, delivery = confirmed
    email = client.post(f'/api/google/interviews/{interview}/deliveries', json={
        'kind': 'email', 'recipient': 'candidate@example.test', 'confirmed': True,
    }).json()['id']
    with connect(path) as db:
        actor = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
        db.execute('INSERT INTO google_calendar_removals(delivery_id,state,actor_id) VALUES (?,?,?)',
                   (delivery, removal_state, actor))
    calls = []
    app.state.google_dispatcher = lambda **kwargs: calls.append(kwargs)
    assert client.post(f'/api/google/deliveries/{email}/send', json={'confirmed': True, 'expected_recipient': 'candidate@example.test'}).status_code == 409
    assert calls == []
    items = client.get(f'/api/google/interviews/{interview}/deliveries').json()
    assert next(x for x in items if x['kind'] == 'calendar')['calendar_removal_state'] == removal_state


def test_reconcile_refuses_old_rows_without_provenance_and_inflight(confirmed):
    client, app, path, interview, delivery = confirmed
    calls = []
    install_remover(app, lambda **kw: calls.append(kw) or {'status': 'removed'})
    url = f'/api/google/deliveries/{delivery}/calendar-removal'
    with connect(path) as db:
        db.execute('UPDATE google_deliveries SET calendar_binding=NULL WHERE id=?', (delivery,))
    assert client.post(url, json={'confirmed': True}).status_code == 409
    with connect(path) as db:
        db.execute("UPDATE google_deliveries SET calendar_binding=?,state='sending' WHERE id=?", ('a' * 64, delivery))
    assert client.post(url, json={'confirmed': True}).status_code == 409
    assert client.post(f'/api/interviews/{interview}/cancel').status_code == 409
    assert not calls


def test_revoked_hr_session_cannot_reconcile(confirmed):
    client, app, path, _, delivery = confirmed
    calls = []
    install_remover(app, lambda **kw: calls.append(kw) or {'status': 'removed'})
    with connect(path) as db:
        db.execute("UPDATE users SET active=0 WHERE username='hr'")
    assert client.post(f'/api/google/deliveries/{delivery}/calendar-removal', json={'confirmed': True}).status_code == 401
    assert not calls


def test_revocation_in_preflight_never_calls_provider(confirmed):
    client, app, path, _, delivery = confirmed
    calls = []
    def preflight():
        with connect(path) as db:
            db.execute("UPDATE users SET active=0 WHERE username='hr'")
        return bound(lambda **kw: calls.append(kw) or {'status':'removed'})
    app.state.google_calendar_removal_preflight = preflight
    assert client.post(f'/api/google/deliveries/{delivery}/calendar-removal', json={'confirmed':True}).status_code == 401
    assert not calls
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM google_calendar_removals').fetchone()[0] == 0


def test_old_schema_is_migrated_without_inventing_provenance(tmp_path):
    from app.google_outbox import initialize_google_outbox

    path = tmp_path / 'legacy.sqlite3'
    with connect(path) as db:
        db.execute('CREATE TABLE google_deliveries (id INTEGER PRIMARY KEY, interview_id INTEGER, state TEXT)')
        db.execute("INSERT INTO google_deliveries VALUES (1, 42, 'confirmed')")
    initialize_google_outbox(path)
    initialize_google_outbox(path)
    with connect(path) as db:
        row = db.execute('SELECT state, calendar_binding, calendar_etag FROM google_deliveries').fetchone()
        assert tuple(row) == ('confirmed', None, None)

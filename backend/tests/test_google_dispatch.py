from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import google_dispatch
from app.db import connect
from app.main import create_app


def session(client='example-client', token='example-refresh'):
    return SimpleNamespace(credentials=SimpleNamespace(client_id=client, refresh_token=token))


def test_calendar_dispatch_and_removal_bind_the_same_captured_credential(monkeypatch):
    sessions = [session(), session()]
    monkeypatch.setattr(google_dispatch, '_authorized_session', lambda kind: sessions.pop(0))
    sent = []
    removed = []
    monkeypatch.setattr(google_dispatch.google_services, 'create_calendar_event',
                        lambda sess, **kw: sent.append((sess, kw)) or {'status': 'confirmed'})
    monkeypatch.setattr(google_dispatch.google_services, 'remove_calendar_event',
                        lambda sess, **kw: removed.append((sess, kw)) or {'status': 'removed'})
    dispatch = google_dispatch.prepare_dispatch('calendar')
    remove = google_dispatch.prepare_calendar_removal()
    assert dispatch.google_credential_binding == remove.google_credential_binding
    assert len(dispatch.google_credential_binding) == 64
    assert 'example-refresh' not in dispatch.google_credential_binding
    assert dispatch(kind='calendar', event_id='abc123', start_at='a', end_at='b', recipient=None)['status'] == 'confirmed'
    assert remove(event_id='abc123', etag='"etag"', start='a', end='b')['status'] == 'removed'
    assert sent[0][1]['event_id'] == removed[0][1]['event_id'] == 'abc123'
    with pytest.raises(ValueError):
        dispatch(kind='email', event_id=None, start_at='a', end_at='b', recipient='a@example.test')


def test_missing_or_rotated_credential_does_not_match(monkeypatch):
    sessions = [session(), session(token='changed'), session(token=None)]
    monkeypatch.setattr(google_dispatch, '_authorized_session', lambda kind: sessions.pop(0))
    dispatch = google_dispatch.prepare_dispatch('calendar')
    remove = google_dispatch.prepare_calendar_removal()
    assert dispatch.google_credential_binding != remove.google_credential_binding
    with pytest.raises(RuntimeError):
        google_dispatch.prepare_calendar_removal()


def test_complete_calendar_flow_uses_same_credential_and_conditional_delete(tmp_path, monkeypatch):
    class Response:
        def __init__(self, code, body):
            self.status_code, self.body = code, body

        def json(self):
            return self.body

    class FakeGoogle:
        def __init__(self):
            self.credentials = SimpleNamespace(client_id='synthetic-client', refresh_token='synthetic-token')
            self.event = None
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, kwargs))
            if method == 'POST':
                self.event = dict(kwargs['json'], etag='"synthetic-version"', status='confirmed')
                return Response(200, self.event)
            if method == 'GET':
                return Response(200, self.event)
            assert method == 'DELETE' and kwargs['headers']['If-Match'] == '"synthetic-version"'
            self.event = None
            return Response(204, {})

    google = FakeGoogle()
    monkeypatch.setattr(google_dispatch, '_authorized_session', lambda kind: google)
    path = tmp_path / 'real-flow.sqlite3'
    app = create_app(path, seed_demo=True)
    app.state.google_preflight = google_dispatch.prepare_dispatch
    app.state.google_calendar_removal_preflight = google_dispatch.prepare_calendar_removal
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username':'hr', 'password':'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?, 'job-backend-python', 'synthetic.docx', 'synthetic')""", (candidate,)).lastrowid
        start = datetime.now(UTC) + timedelta(days=3)
        interview = client.post('/api/interviews', json={'application_id':application, 'start_at':start.isoformat(),
            'end_at':(start+timedelta(hours=1)).isoformat(), 'interviewer':'Synthetic HR'}).json()['id']
        delivery = client.post(f'/api/google/interviews/{interview}/deliveries',
            json={'kind':'calendar', 'confirmed':True}).json()['id']
        assert client.post(f'/api/google/deliveries/{delivery}/send', json={'confirmed':True}).json()['state'] == 'confirmed'
        assert client.post(f'/api/interviews/{interview}/cancel').status_code == 409
        result = client.post(f'/api/google/deliveries/{delivery}/calendar-removal', json={'confirmed':True})
        assert result.json()['state'] == 'removed'
        assert client.post(f'/api/interviews/{interview}/cancel').status_code == 200
        assert [call[0] for call in google.calls] == ['POST', 'GET', 'DELETE']

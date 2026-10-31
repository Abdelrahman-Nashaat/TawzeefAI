import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import scheduling, slots
from app.db import connect
from app.main import create_app


@pytest.fixture
def state(tmp_path):
    path = tmp_path / 'scheduling.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','x.pdf','synthetic','advanced')", (candidate,)).lastrowid
        yield client, path, app_id


def login(client, role):
    assert client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'}).status_code == 200


def interval():
    start = datetime.now(UTC) + timedelta(days=3)
    return {'start_at': start.isoformat(), 'end_at': (start + timedelta(hours=1)).isoformat(), 'interviewer': 'Reviewer'}


def expire_at_commit(monkeypatch, module):
    now = time.time()
    readings = iter([now, now + 365 * 86400])
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: next(readings)))


def test_hr_create_rollback_when_session_expires_before_commit(state, monkeypatch):
    client, path, app_id = state
    login(client, 'hr')
    expire_at_commit(monkeypatch, scheduling)
    result = client.post('/api/interviews', json={**interval(), 'application_id': app_id})
    assert result.status_code == 401
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM interviews').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='interview_scheduled'").fetchone()[0] == 0


def test_hr_cancel_rollback_when_session_expires_before_commit(state, monkeypatch):
    client, path, app_id = state
    login(client, 'hr')
    result = client.post('/api/interviews', json={**interval(), 'application_id': app_id})
    assert result.status_code == 201
    expire_at_commit(monkeypatch, scheduling)
    result = client.post(f"/api/interviews/{result.json()['id']}/cancel")
    assert result.status_code == 401
    with connect(path) as db:
        assert db.execute('SELECT status FROM interviews').fetchone()[0] == 'scheduled'
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='interview_cancelled'").fetchone()[0] == 0


def test_candidate_booking_rollback_when_session_expires_before_commit(state, monkeypatch):
    client, path, app_id = state
    login(client, 'hr')
    slot = client.post('/api/slots', json=interval())
    assert slot.status_code == 201
    login(client, 'candidate')
    expire_at_commit(monkeypatch, slots)
    result = client.post(f"/api/slots/{slot.json()['id']}/book", json={'application_id': app_id})
    assert result.status_code == 401
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM interviews').fetchone()[0] == 0
        assert db.execute('SELECT interview_id FROM interview_slots').fetchone()[0] is None
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='interview_scheduled'").fetchone()[0] == 0

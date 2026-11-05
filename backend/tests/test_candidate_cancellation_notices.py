import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.db import connect
from app.main import create_app


@pytest.fixture
def state(tmp_path):
    app = create_app(tmp_path / 'notices.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        with connect(app.state.database_path) as db:
            owner = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            db.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES('other','Other','candidate',?)", (hash_password('other-password'),))
            app_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?, 'job-backend-python', 'cv.docx','sample','advanced')", (owner,)).lastrowid
        yield app, client, app_id

def login(client, name, password=None):
    assert client.post('/api/auth/login', json={'username': name, 'password': password or f'{name}-demo-only'}).status_code == 200

def interview(client, app_id):
    start = datetime.now(UTC) + timedelta(days=1)
    result = client.post('/api/interviews', json={'application_id': app_id, 'start_at': start.isoformat(), 'end_at': (start + timedelta(hours=1)).isoformat(), 'interviewer': 'HR'})
    assert result.status_code == 201
    return result.json()['id']

@pytest.mark.parametrize('path', ['cancel', 'reject'])
def test_notification_owner_and_acknowledgement(state, path):
    app, client, app_id = state
    assert client.get('/api/candidate/interview-cancellations').status_code == 401
    login(client, 'hr')
    meeting = interview(client, app_id)
    assert client.get('/api/candidate/interview-cancellations').status_code == 403
    if path == 'cancel':
        assert client.post(f'/api/interviews/{meeting}/cancel').status_code == 200
    else:
        assert client.post(f'/api/applications/{app_id}/decision', json={'decision': 'reject', 'note': 'Internal note'}).status_code == 200
    login(client, 'candidate')
    data = client.get('/api/candidate/interview-cancellations').json()
    assert len(data['items']) == 1
    notice = data['items'][0]
    assert notice['application_id'] == app_id and notice['interview_id'] == meeting
    assert not notice['is_read'] and 'Internal note' not in str(data)
    assert client.post(f"/api/candidate/interview-cancellations/{notice['id']}/read").status_code == 200
    assert client.post(f"/api/candidate/interview-cancellations/{notice['id']}/read").status_code == 200
    assert client.get('/api/candidate/interview-cancellations').json()['items'][0]['is_read']
    login(client, 'other', 'other-password')
    assert client.get('/api/candidate/interview-cancellations').json()['items'] == []
    assert client.post(f"/api/candidate/interview-cancellations/{notice['id']}/read").status_code == 404
    with connect(app.state.database_path) as db:
        assert db.execute('SELECT count(*) FROM google_deliveries').fetchone()[0] == 0

@pytest.mark.parametrize('path', ['cancel', 'reject'])
def test_notice_failure_rolls_back(state, path):
    app, client, app_id = state
    login(client, 'hr')
    meeting = interview(client, app_id)
    with connect(app.state.database_path) as db:
        db.execute("CREATE TRIGGER fail_notice BEFORE INSERT ON hr_interview_cancellations BEGIN SELECT RAISE(ABORT,'failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        if path == 'cancel':
            client.post(f'/api/interviews/{meeting}/cancel')
        else:
            client.post(f'/api/applications/{app_id}/decision', json={'decision': 'reject', 'note': 'Reason'})
    with connect(app.state.database_path) as db:
        assert db.execute('SELECT status FROM interviews WHERE id=?', (meeting,)).fetchone()[0] == 'scheduled'
        assert db.execute('SELECT status FROM applications WHERE id=?', (app_id,)).fetchone()[0] == 'advanced'
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='interview_cancelled'").fetchone()[0] == 0

def test_pagination_and_candidate_cancellation_do_not_create_hr_notices(state):
    _app, client, app_id = state
    login(client, 'hr')
    first = interview(client, app_id)
    assert client.post(f'/api/interviews/{first}/cancel').status_code == 200
    second = interview(client, app_id)
    assert client.post(f'/api/interviews/{second}/cancel').status_code == 200
    third = interview(client, app_id)
    login(client, 'candidate')
    assert client.post(f'/api/interviews/{third}/candidate-cancel').status_code == 200
    newest = client.get('/api/candidate/interview-cancellations?limit=1').json()
    older = client.get('/api/candidate/interview-cancellations', params={'limit': 1, 'before': newest['next_before']}).json()
    assert newest['items'][0]['interview_id'] == second
    assert older['items'][0]['interview_id'] == first and older['next_before'] is None


def test_ownership_change_revokes_access_to_old_notice(state):
    app, client, app_id = state
    login(client, 'hr')
    meeting = interview(client, app_id)
    assert client.post(f'/api/interviews/{meeting}/cancel').status_code == 200
    login(client, 'candidate')
    notice = client.get('/api/candidate/interview-cancellations').json()['items'][0]['id']
    with connect(app.state.database_path) as db:
        other = db.execute("SELECT id FROM users WHERE username='other'").fetchone()[0]
        db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (other, app_id))
    assert client.get('/api/candidate/interview-cancellations').json()['items'] == []
    assert client.post(f'/api/candidate/interview-cancellations/{notice}/read').status_code == 404
    login(client, 'other', 'other-password')
    assert client.get('/api/candidate/interview-cancellations').json()['items'] == []
    assert client.post(f'/api/candidate/interview-cancellations/{notice}/read').status_code == 404

def test_fresh_candidate_authorization_and_ownership_rechecked(state, monkeypatch):
    from app import scheduling

    app, client, app_id = state
    login(client, 'hr')
    meeting = interview(client, app_id)
    assert client.post(f'/api/interviews/{meeting}/cancel').status_code == 200
    login(client, 'candidate')
    original = scheduling._require_current_candidate
    calls = 0

    def change_owner_after_snapshot(db, request, user):
        nonlocal calls
        calls += 1
        if calls == 2:
            with connect(app.state.database_path) as writer:
                other = writer.execute("SELECT id FROM users WHERE username='other'").fetchone()[0]
                writer.execute('UPDATE applications SET candidate_id=? WHERE id=?', (other, app_id))
        return original(db, request, user)

    monkeypatch.setattr(scheduling, '_require_current_candidate', change_owner_after_snapshot)
    assert client.get('/api/candidate/interview-cancellations').status_code == 409

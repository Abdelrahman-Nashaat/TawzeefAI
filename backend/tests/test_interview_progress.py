import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import scheduling
from app.db import connect
from app.main import create_app
from app.scheduling import utc_text


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / 'progress.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            application = db.execute("INSERT INTO applications(candidate_id, job_id, filename, resume_text, status) VALUES (?, 'job-backend-python', 'test.pdf', 'sample', 'advanced')", (candidate,)).lastrowid
            now = datetime.now(UTC)
            interview = db.execute("INSERT INTO interviews(application_id,candidate_id,start_at,end_at,interviewer,interviewer_key,created_by) VALUES(?,?,?,?,?,?,?)", (application,candidate,utc_text(now-timedelta(hours=2)),utc_text(now-timedelta(hours=1)),'Mona','mona',hr)).lastrowid
        yield app, client, application, interview


def login(client, user):
    assert client.post('/api/auth/login', json={'username': user, 'password': f'{user}-demo-only'}).status_code == 200


def test_completed_interview_is_explicit_audited_and_candidate_visible(setup):
    app, client, app_id, interview_id = setup
    login(client, 'candidate')
    before = client.get(f'/api/workflows/{app_id}/progress')
    assert before.status_code == 200 and before.json() == {'application_id': app_id, 'stage': 'interview_scheduled'}
    assert client.post(f'/api/interviews/{interview_id}/complete').status_code == 403
    login(client, 'hr')
    assert client.get(f'/api/workflows/{app_id}').json()['interview']['completed'] is False
    response = client.post(f'/api/interviews/{interview_id}/complete')
    assert response.status_code == 200 and response.json()['completed'] is True
    workflow = client.get(f'/api/workflows/{app_id}').json()
    assert workflow['interview']['completed'] is True
    assert client.post(f'/api/interviews/{interview_id}/complete').status_code == 409
    with connect(app.state.database_path) as db:
        assert db.execute('SELECT count(*) FROM interview_completions WHERE interview_id=?', (interview_id,)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='interview_completed' AND application_id=?", (app_id,)).fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM google_deliveries').fetchone()[0] == 0
    login(client, 'candidate')
    assert client.get(f'/api/workflows/{app_id}/progress').json() == {'application_id': app_id, 'stage': 'interviewed'}


def test_future_cancelled_or_finalized_cannot_be_completed(setup):
    app, client, app_id, interview_id = setup
    login(client, 'hr')
    with connect(app.state.database_path) as db:
        db.execute('UPDATE interviews SET end_at=? WHERE id=?', (utc_text(datetime.now(UTC)+timedelta(hours=1)), interview_id))
    assert client.post(f'/api/interviews/{interview_id}/complete').status_code == 409
    with connect(app.state.database_path) as db:
        db.execute("UPDATE interviews SET status='cancelled', end_at=? WHERE id=?", (utc_text(datetime.now(UTC)-timedelta(hours=1)), interview_id))
    assert client.post(f'/api/interviews/{interview_id}/complete').status_code == 409
    with connect(app.state.database_path) as db:
        db.execute("UPDATE interviews SET status='scheduled' WHERE id=?", (interview_id,))
        db.execute("UPDATE applications SET status='rejected' WHERE id=?", (app_id,))
    assert client.post(f'/api/interviews/{interview_id}/complete').status_code == 409
    login(client, 'candidate')
    assert client.get(f'/api/workflows/{app_id}/progress').json()['stage'] == 'rejected'


def test_completion_race_records_once(setup):
    app, client, app_id, interview_id = setup
    login(client, 'hr')
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: client.post(f'/api/interviews/{interview_id}/complete').status_code, range(2)))
    assert sorted(statuses) == [200, 409]
    with connect(app.state.database_path) as db:
        assert db.execute('SELECT count(*) FROM interview_completions WHERE interview_id=?', (interview_id,)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='interview_completed'", (app_id,)).fetchone()[0] == 1


def test_progress_rejects_other_candidate_and_employee(setup):
    app, client, app_id, _interview_id = setup
    with connect(app.state.database_path) as db:
        db.execute("UPDATE applications SET candidate_id=(SELECT id FROM users WHERE username='employee') WHERE id=?", (app_id,))
    login(client, 'candidate')
    assert client.get(f'/api/workflows/{app_id}/progress').status_code == 404
    login(client, 'employee')
    assert client.get(f'/api/workflows/{app_id}/progress').status_code == 403


def test_audit_failure_rolls_back_completion(setup):
    app, client, _app_id, interview_id = setup
    login(client, 'hr')
    with connect(app.state.database_path) as db:
        db.execute("CREATE TRIGGER abort_completion_audit BEFORE INSERT ON audit_events WHEN NEW.action='interview_completed' BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='audit failure'):
        client.post(f'/api/interviews/{interview_id}/complete')
    with connect(app.state.database_path) as db:
        assert db.execute('SELECT count(*) FROM interview_completions').fetchone()[0] == 0


def test_session_expiry_before_commit_rolls_back(setup, monkeypatch):
    app, client, _app_id, interview_id = setup
    login(client, 'hr')
    now = time.time()
    readings = iter([now, now + 60 * 60 * 24 * 365])
    monkeypatch.setattr(scheduling, 'time', SimpleNamespace(time=lambda: next(readings)))
    assert client.post(f'/api/interviews/{interview_id}/complete').status_code == 401
    with connect(app.state.database_path) as db:
        assert db.execute('SELECT count(*) FROM interview_completions').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='interview_completed'").fetchone()[0] == 0


def test_newer_interview_and_final_decision_take_precedence(setup):
    app, client, app_id, interview_id = setup
    login(client, 'hr')
    assert client.post(f'/api/interviews/{interview_id}/complete').status_code == 200
    start = datetime.now(UTC) + timedelta(days=1)
    new_slot = client.post('/api/slots', json={'start_at': start.isoformat(), 'end_at': (start+timedelta(hours=1)).isoformat(), 'interviewer': 'Mona'})
    assert new_slot.status_code == 201
    login(client, 'candidate')
    assert [slot['id'] for slot in client.get('/api/slots', params={'application_id': app_id}).json()] == [new_slot.json()['id']]
    with ThreadPoolExecutor(max_workers=2) as pool:
        bookings = list(pool.map(lambda _: client.post(f"/api/slots/{new_slot.json()['id']}/book", json={'application_id': app_id}), range(2)))
    assert sorted(result.status_code for result in bookings) == [201, 409]
    next_id = next(result.json()['id'] for result in bookings if result.status_code == 201)
    assert client.get(f'/api/workflows/{app_id}/progress').json()['stage'] == 'interview_scheduled'
    login(client, 'hr')
    assert client.post(f'/api/interviews/{next_id}/cancel').status_code == 200
    login(client, 'candidate')
    assert client.get(f'/api/workflows/{app_id}/progress').json()['stage'] == 'interviewed'
    with connect(app.state.database_path) as db:
        db.execute("UPDATE applications SET status='hired' WHERE id=?", (app_id,))
    assert client.get(f'/api/workflows/{app_id}/progress').json()['stage'] == 'hired'


def test_hr_can_schedule_next_interview_only_after_completion(setup):
    _app, client, app_id, interview_id = setup
    login(client, 'hr')
    start = datetime.now(UTC) + timedelta(days=1)
    payload = {'application_id': app_id, 'start_at': start.isoformat(), 'end_at': (start+timedelta(hours=1)).isoformat(), 'interviewer': 'Mona'}
    assert client.post('/api/interviews', json=payload).status_code == 409
    assert client.post(f'/api/interviews/{interview_id}/complete').status_code == 200
    assert client.post('/api/interviews', json=payload).status_code == 201
    assert client.post('/api/interviews', json=payload).status_code == 409


def test_initialize_adds_completion_storage_for_legacy_interviews(tmp_path):
    from app.scheduling import initialize_scheduling

    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE interviews (id INTEGER PRIMARY KEY, application_id INTEGER,
            candidate_id INTEGER, start_at TEXT, end_at TEXT, interviewer TEXT, interviewer_key TEXT,
            created_by INTEGER, created_at TEXT);
            INSERT INTO interviews VALUES(1, 1, 1, '2024-01-01T00:00:00Z', '2024-01-01T01:00:00Z', 'M', 'm', 1, '2024-01-01');''')
    initialize_scheduling(path)
    initialize_scheduling(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT id,status FROM interviews').fetchall() == [(1, 'scheduled')]
        assert db.execute('SELECT count(*) FROM interview_completions').fetchone()[0] == 0

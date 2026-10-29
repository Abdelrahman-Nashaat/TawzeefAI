import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.db import connect
from app.main import create_app


@pytest.fixture
def state(tmp_path):
    path = tmp_path / 'slots.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()['id']
            other = db.execute(
                'INSERT INTO users (username, display_name, role, password_hash) VALUES (?, ?, ?, ?)',
                ('other', 'Other', 'candidate', hash_password('other-password')),
            ).lastrowid
            first = db.execute('''INSERT INTO applications (candidate_id,job_id,filename,resume_text,status)
                VALUES (?, 'job-backend-python', 'one.docx', 'Sample', 'advanced')''', (candidate,)).lastrowid
            second = db.execute('''INSERT INTO applications (candidate_id,job_id,filename,resume_text,status)
                VALUES (?, 'job-backend-python', 'two.docx', 'Sample', 'advanced')''', (other,)).lastrowid
        yield app, client, first, second


def login(client, name, password=None):
    result = client.post('/api/auth/login', json={
        'username': name, 'password': password or f'{name}-demo-only',
    })
    assert result.status_code == 200


def slot(client, shift=0):
    start = datetime.now(UTC) + timedelta(days=1, hours=shift)
    response = client.post('/api/slots', json={'start_at': start.isoformat(),
        'end_at': (start + timedelta(hours=1)).isoformat(), 'interviewer': 'Mona'})
    assert response.status_code == 201
    return response.json()['id']


def test_authorization_and_candidate_booking_are_audited(state):
    app, client, first, second = state
    assert client.get('/api/slots').status_code == 401
    login(client, 'hr')
    slot_id = slot(client)
    assert client.post(f'/api/slots/{slot_id}/book', json={'application_id': first}).status_code == 403
    login(client, 'candidate')
    assert client.post('/api/slots', json={}).status_code == 403
    assert client.get('/api/slots', params={'application_id': second}).status_code == 404
    assert client.post(f'/api/slots/{slot_id}/book', json={'application_id': second}).status_code == 404
    assert [s['id'] for s in client.get('/api/slots', params={'application_id': first}).json()] == [slot_id]
    booking = client.post(f'/api/slots/{slot_id}/book', json={'application_id': first})
    assert booking.status_code == 201 and booking.json()['application_id'] == first
    assert client.get('/api/slots', params={'application_id': first}).json() == []
    assert client.post(f'/api/slots/{slot_id}/book', json={'application_id': first}).status_code == 409
    with connect(app.state.database_path) as db:
        assert db.execute("SELECT count(*) FROM slot_audit_events WHERE action='slot_created'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='interview_scheduled'").fetchone()[0] == 1
    login(client, 'employee')
    assert client.get('/api/slots', params={'application_id': first}).status_code == 403


def test_application_must_be_advanced_and_rejected_candidates_cannot_book(state):
    app, client, first, _ = state
    login(client, 'hr')
    slot_id = slot(client)
    with connect(app.state.database_path) as db:
        db.execute("UPDATE applications SET status='submitted' WHERE id=?", (first,))
    login(client, 'candidate')
    assert client.get('/api/slots', params={'application_id': first}).status_code == 409
    assert client.post(f'/api/slots/{slot_id}/book', json={'application_id': first}).status_code == 409
    with connect(app.state.database_path) as db:
        db.execute("UPDATE applications SET status='rejected' WHERE id=?", (first,))
    assert client.post(f'/api/slots/{slot_id}/book', json={'application_id': first}).status_code == 409


def test_same_slot_race_only_books_once(state):
    app, client, first, second = state
    login(client, 'hr')
    slot_id = slot(client)
    with TestClient(app) as candidate_a, TestClient(app) as candidate_b:
        login(candidate_a, 'candidate')
        login(candidate_b, 'other', 'other-password')
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_request = executor.submit(candidate_a.post, f'/api/slots/{slot_id}/book', json={'application_id': first})
            second_request = executor.submit(candidate_b.post, f'/api/slots/{slot_id}/book', json={'application_id': second})
            assert sorted([first_request.result().status_code, second_request.result().status_code]) == [201, 409]
    with connect(app.state.database_path) as db:
        assert db.execute("SELECT count(*) FROM interviews WHERE status='scheduled'").fetchone()[0] == 1


def test_interview_cancel_releases_slot_and_failed_audit_rolls_back(state):
    app, client, first, second = state
    login(client, 'hr')
    slot_id = slot(client)
    login(client, 'candidate')
    interview = client.post(f'/api/slots/{slot_id}/book', json={'application_id': first}).json()
    login(client, 'hr')
    assert client.post(f"/api/interviews/{interview['id']}/cancel").status_code == 200
    login(client, 'other', 'other-password')
    assert [s['id'] for s in client.get('/api/slots', params={'application_id': second}).json()] == [slot_id]
    with connect(app.state.database_path) as db:
        db.execute("CREATE TRIGGER abort_slot_audit BEFORE INSERT ON audit_events WHEN NEW.action = 'interview_scheduled' BEGIN SELECT RAISE(ABORT, 'audit failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match='audit failed'):
        client.post(f'/api/slots/{slot_id}/book', json={'application_id': second})
    with connect(app.state.database_path) as db:
        assert db.execute("SELECT count(*) FROM interviews WHERE status='scheduled'").fetchone()[0] == 0
        assert db.execute('SELECT interview_id FROM interview_slots WHERE id=?', (slot_id,)).fetchone()[0] == interview['id']


def test_timezone_validation_and_conflicting_interviewer(state):
    _, client, first, _ = state
    login(client, 'hr')
    assert client.post('/api/slots', json={'start_at': '2035-01-01T10:00:00',
        'end_at': '2035-01-01T11:00:00Z', 'interviewer': 'Mona'}).status_code == 422
    past = datetime.now(UTC) - timedelta(days=1)
    assert client.post('/api/slots', json={'start_at': past.isoformat(),
        'end_at': (past + timedelta(hours=1)).isoformat(), 'interviewer': 'Mona'}).status_code == 422
    slot_id = slot(client)
    original = client.get('/api/slots').json()[0]
    # The manual HR path can reserve the interviewer after slot creation.
    assert client.post('/api/interviews', json={'application_id': first,
        'start_at': original['start_at'], 'end_at': original['end_at'], 'interviewer': 'Mona'}).status_code == 201
    login(client, 'candidate')
    assert client.get('/api/slots', params={'application_id': first}).json() == []
    assert client.post(f'/api/slots/{slot_id}/book', json={'application_id': first}).status_code == 409


def test_hr_withdraws_available_slot_and_candidate_cannot_book(state):
    app, client, first, _ = state
    login(client, 'hr')
    slot_id = slot(client)
    login(client, 'candidate')
    assert client.post(f'/api/slots/{slot_id}/withdraw').status_code == 403
    login(client, 'hr')
    result = client.post(f'/api/slots/{slot_id}/withdraw')
    assert result.status_code == 200 and result.json()['withdrawn'] is True
    assert client.post(f'/api/slots/{slot_id}/withdraw').status_code == 409
    assert client.get('/api/slots').json()[0]['withdrawn'] is True
    login(client, 'candidate')
    assert client.get('/api/slots', params={'application_id': first}).json() == []
    assert client.post(f'/api/slots/{slot_id}/book', json={'application_id': first}).status_code == 409
    with connect(app.state.database_path) as db:
        assert db.execute("SELECT count(*) FROM slot_audit_events WHERE slot_id=? AND action='slot_withdrawn'", (slot_id,)).fetchone()[0] == 1


def test_withdraw_does_not_cancel_a_booked_interview_and_failed_audit_rolls_back(state):
    app, client, first, _ = state
    login(client, 'hr')
    booked = slot(client)
    available = slot(client, shift=2)
    login(client, 'candidate')
    assert client.post(f'/api/slots/{booked}/book', json={'application_id': first}).status_code == 201
    login(client, 'hr')
    assert client.post(f'/api/slots/{booked}/withdraw').status_code == 409
    with connect(app.state.database_path) as db:
        db.execute("CREATE TRIGGER fail_slot_withdraw BEFORE INSERT ON slot_audit_events WHEN NEW.action='slot_withdrawn' BEGIN SELECT RAISE(ABORT, 'audit failed'); END")
    with pytest.raises(sqlite3.IntegrityError, match='audit failed'):
        client.post(f'/api/slots/{available}/withdraw')
    with connect(app.state.database_path) as db:
        assert db.execute('SELECT withdrawn FROM interview_slots WHERE id=?', (available,)).fetchone()[0] == 0


def test_withdrawal_racing_booking_has_one_winner(state):
    app, client, first, _ = state
    login(client, 'hr')
    slot_id = slot(client)
    with TestClient(app) as hr, TestClient(app) as candidate:
        login(hr, 'hr')
        login(candidate, 'candidate')
        with ThreadPoolExecutor(max_workers=2) as executor:
            withdraw = executor.submit(hr.post, f'/api/slots/{slot_id}/withdraw')
            booking = executor.submit(candidate.post, f'/api/slots/{slot_id}/book', json={'application_id': first})
            statuses = withdraw.result().status_code, booking.result().status_code
    assert statuses in ((200, 409), (409, 201))
    with connect(app.state.database_path) as db:
        row = db.execute('SELECT withdrawn, interview_id FROM interview_slots WHERE id=?', (slot_id,)).fetchone()
        assert (row['withdrawn'] == 1) != (row['interview_id'] is not None)


def test_withdraw_rechecks_revoked_hr_in_write_transaction(state):
    from app.auth import User, get_current_user

    app, client, _, _ = state
    login(client, 'hr')
    slot_id = slot(client)
    def revoked_user():
        with connect(app.state.database_path) as db:
            row = db.execute("SELECT id,username,display_name FROM users WHERE username='hr'").fetchone()
            db.execute('UPDATE users SET active=0 WHERE id=?', (row['id'],))
        return User(id=row['id'], username=row['username'], display_name=row['display_name'], role='hr')
    app.dependency_overrides[get_current_user] = revoked_user
    try:
        assert client.post(f'/api/slots/{slot_id}/withdraw').status_code == 401
    finally:
        app.dependency_overrides.clear()
    with connect(app.state.database_path) as db:
        assert db.execute('SELECT withdrawn FROM interview_slots WHERE id=?', (slot_id,)).fetchone()[0] == 0


def test_legacy_slot_schema_is_migrated_without_withdrawing_existing_slots(tmp_path):
    from app.slots import initialize_slots

    path = tmp_path / 'legacy.sqlite3'
    with connect(path) as db:
        db.executescript('''CREATE TABLE interview_slots (id INTEGER PRIMARY KEY, start_at TEXT NOT NULL,
            end_at TEXT NOT NULL, interviewer TEXT NOT NULL, interviewer_key TEXT NOT NULL,
            created_by INTEGER NOT NULL, interview_id INTEGER, created_at TEXT NOT NULL);
            INSERT INTO interview_slots VALUES (1,'2035-01-01T10:00:00Z','2035-01-01T11:00:00Z',
                'HR','hr',1,NULL,'2035-01-01T00:00:00Z');''')
    initialize_slots(path)
    initialize_slots(path)
    with connect(path) as db:
        assert db.execute('SELECT withdrawn FROM interview_slots WHERE id=1').fetchone()[0] == 0

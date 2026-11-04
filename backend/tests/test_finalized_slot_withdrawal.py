import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app
from app.scheduling import utc_text


@pytest.fixture
def context(tmp_path):
    path = tmp_path / 'finalization.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as hr:
        assert hr.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            actor = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            application = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?, 'job-backend-python', 'cv.pdf', 'synthetic', 'advanced')", (candidate,)).lastrowid
        yield path, app, hr, application, candidate, actor


def add_slot(path, actor, application, candidate, interviewer='Mona'):
    start = datetime.now(UTC) + timedelta(days=5)
    with connect(path) as db:
        return db.execute('''INSERT INTO interview_slots(start_at,end_at,interviewer,interviewer_key,created_by,target_application_id,target_candidate_id)
            VALUES (?,?,?,?,?,?,?)''', (utc_text(start), utc_text(start + timedelta(minutes=30)), interviewer,
                                        interviewer.casefold(), actor, application, candidate)).lastrowid


def test_reject_releases_only_targeted_availability_and_audits(context):
    path, _, hr, application, candidate, actor = context
    private = add_slot(path, actor, application, candidate)
    general = add_slot(path, actor, None, None, 'Other')
    assert hr.post(f'/api/applications/{application}/decision', json={'decision': 'reject'}).status_code == 200
    with connect(path) as db:
        assert db.execute('SELECT withdrawn FROM interview_slots WHERE id=?', (private,)).fetchone()[0] == 1
        assert db.execute('SELECT withdrawn FROM interview_slots WHERE id=?', (general,)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM slot_audit_events WHERE slot_id=? AND action='slot_withdrawn'", (private,)).fetchone()[0] == 1


def test_audit_failure_rolls_back_decision_and_slot(context):
    path, _, hr, application, candidate, actor = context
    private = add_slot(path, actor, application, candidate)
    with connect(path) as db:
        db.execute("CREATE TRIGGER fail_withdraw BEFORE INSERT ON slot_audit_events WHEN NEW.action='slot_withdrawn' BEGIN SELECT RAISE(ABORT, 'failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        hr.post(f'/api/applications/{application}/decision', json={'decision': 'reject'})
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'advanced'
        assert db.execute('SELECT withdrawn FROM interview_slots WHERE id=?', (private,)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (application,)).fetchone()[0] == 0


def test_hire_releases_private_availability_after_accepted_offer(context):
    path, _, hr, application, candidate, actor = context
    private = add_slot(path, actor, application, candidate)
    with connect(path) as db:
        db.execute("UPDATE applications SET status='offered' WHERE id=?", (application,))
        db.execute("INSERT INTO job_offers(application_id,recipient_id,details,status,created_by) VALUES (?, ?, 'synthetic', 'accepted', ?)", (application, candidate, actor))
    assert hr.post(f'/api/applications/{application}/decision', json={'decision': 'hire'}).status_code == 200
    with connect(path) as db:
        assert db.execute('SELECT withdrawn FROM interview_slots WHERE id=?', (private,)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM slot_audit_events WHERE slot_id=? AND action='slot_withdrawn'", (private,)).fetchone()[0] == 1


def test_google_reconciliation_block_rolls_back_rejection(context, monkeypatch):
    from app import recruitment

    path, _, hr, application, candidate, actor = context
    private = add_slot(path, actor, application, candidate)
    start = datetime.now(UTC) + timedelta(days=6)
    with connect(path) as db:
        db.execute('''INSERT INTO interviews(application_id,candidate_id,start_at,end_at,interviewer,interviewer_key,created_by)
            VALUES (?,?,?,?,?,?,?)''', (application, candidate, utc_text(start), utc_text(start + timedelta(minutes=30)), 'Other', 'other', actor))
    monkeypatch.setattr(recruitment, 'cancellation_block_reason', lambda *_: 'unresolved delivery')
    assert hr.post(f'/api/applications/{application}/decision', json={'decision': 'reject'}).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'advanced'
        assert db.execute('SELECT withdrawn FROM interview_slots WHERE id=?', (private,)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM slot_audit_events').fetchone()[0] == 0


def test_candidate_booking_and_rejection_serialize(context):
    path, app, hr, application, candidate, actor = context
    private = add_slot(path, actor, application, candidate)
    with TestClient(app) as candidate_client:
        assert candidate_client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
        with ThreadPoolExecutor(max_workers=2) as pool:
            booking = pool.submit(candidate_client.post, f'/api/slots/{private}/book', json={'application_id': application})
            rejection = pool.submit(hr.post, f'/api/applications/{application}/decision', json={'decision': 'reject'})
            assert rejection.result().status_code == 200
            assert booking.result().status_code in (201, 409)
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'rejected'
        assert db.execute('SELECT withdrawn FROM interview_slots WHERE id=?', (private,)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM interviews WHERE application_id=? AND status='scheduled'", (application,)).fetchone()[0] == 0

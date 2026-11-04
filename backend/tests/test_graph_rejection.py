"""Rejection review binds the reason, recipient, and local effects."""

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.fixture
def case(tmp_path):
    path = tmp_path / 'reject.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            app = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','synthetic.pdf','fictional','advanced')", (candidate,)).lastrowid
            interview = db.execute("""INSERT INTO interviews(application_id,candidate_id,start_at,end_at,interviewer,interviewer_key,status,created_by)
                VALUES (?,?,'2099-01-01T09:00:00Z','2099-01-01T10:00:00Z','Fictional','fictional','scheduled',?)""", (app, candidate, hr)).lastrowid
            slot = db.execute("""INSERT INTO interview_slots(start_at,end_at,interviewer,interviewer_key,created_by,interview_id,target_application_id,target_candidate_id)
                VALUES ('2099-01-01T09:00:00Z','2099-01-01T10:00:00Z','Fictional','fictional',?,?,?,?)""", (hr, interview, app, candidate)).lastrowid
        assert client.post('/api/auth/login', json={'username':'hr','password':'hr-demo-only'}).status_code == 200
        yield client, path, app, candidate, hr, interview, slot


def begin(client, app):
    response = client.post('/api/graph-reviews', json={'intent':'reject_application','application_id':app,'rejection_reason':'Synthetic review reason'})
    assert response.status_code == 201, response.text
    return response.json()


def decide(client, review, approved=True):
    body = {'reviewed': approved}
    if approved:
        body['approval_fingerprint'] = review['state']['action_proposal']['approval_fingerprint']
    return client.post(f"/api/graph-reviews/{review['id']}/resume", json=body)


def test_reject_review_shows_exact_effects_and_commits_once(case):
    client, path, app, candidate, _, interview, slot = case
    review = begin(client, app)
    preview = review['state']['rejection_preview']
    assert preview['candidate_id'] == candidate
    assert preview['reason'] == 'Synthetic review reason'
    assert [item['id'] for item in preview['interviews']] == [interview]
    assert [item['id'] for item in preview['slots']] == [slot]
    with connect(path) as db:
        for table in ('checkpoints', 'writes'):
            for row in db.execute(f'SELECT * FROM {table}'):
                assert all('Synthetic review reason' not in str(value) for value in row)
    result = decide(client, review)
    assert result.status_code == 200, result.text
    assert result.json()['state']['action_result']['status'] == 'rejected'
    assert decide(client, review).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (app,)).fetchone()[0] == 'rejected'
        assert db.execute('SELECT status FROM interviews WHERE id=?', (interview,)).fetchone()[0] == 'cancelled'
        assert db.execute('SELECT withdrawn FROM interview_slots WHERE id=?', (slot,)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (app,)).fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM graph_rejection_drafts').fetchone()[0] == 0


@pytest.mark.parametrize('change', ['owner', 'job', 'slot', 'interview', 'reason'])
def test_changed_and_restored_context_is_stale(case, change):
    client, path, app, candidate, hr, interview, slot = case
    review = begin(client, app)
    with connect(path) as db:
        if change == 'owner':
            db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (hr, app))
            db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (candidate, app))
        elif change == 'job':
            title = db.execute("SELECT title FROM jobs WHERE id='job-backend-python'").fetchone()[0]
            db.execute("UPDATE jobs SET title='Changed' WHERE id='job-backend-python'")
            db.execute("UPDATE jobs SET title=? WHERE id='job-backend-python'", (title,))
        elif change == 'slot':
            db.execute('UPDATE interview_slots SET withdrawn=1 WHERE id=?', (slot,))
            db.execute('UPDATE interview_slots SET withdrawn=0 WHERE id=?', (slot,))
        elif change == 'interview':
            db.execute("UPDATE interviews SET status='cancelled' WHERE id=?", (interview,))
            db.execute("UPDATE interviews SET status='scheduled' WHERE id=?", (interview,))
        else:
            db.execute("UPDATE graph_rejection_drafts SET reason='Changed' WHERE thread_id=?", (review['id'],))
            db.execute("UPDATE graph_rejection_drafts SET reason='Synthetic review reason' WHERE thread_id=?", (review['id'],))
    assert client.get(f"/api/graph-reviews/{review['id']}").json()['state']['rejection_preview']['status'] == 'stale'
    assert decide(client, review).status_code == 409
    assert decide(client, review, False).status_code == 200
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (app,)).fetchone()[0] == 'advanced'
        assert db.execute('SELECT count(*) FROM graph_rejection_drafts').fetchone()[0] == 0


def test_blocked_google_delivery_prevents_rejection(case):
    client, path, app, _, hr, interview, _ = case
    with connect(path) as db:
        db.execute("""INSERT INTO google_deliveries(interview_id,application_id,kind,recipient,start_at,end_at,state,created_by)
            VALUES (?,?,'email','fictional@example.com','2099-01-01T09:00:00Z','2099-01-01T10:00:00Z','sending',?)""", (interview, app, hr))
    response = client.post('/api/graph-reviews', json={'intent':'reject_application','application_id':app,'rejection_reason':'Synthetic'})
    assert response.status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (app,)).fetchone()[0] == 'advanced'
        assert db.execute('SELECT count(*) FROM graph_rejection_drafts').fetchone()[0] == 0


def test_accepted_offer_is_shown_and_not_rewritten(case):
    client, path, app, candidate, hr, _, _ = case
    with connect(path) as db:
        db.execute("UPDATE applications SET status='offered' WHERE id=?", (app,))
        offer = db.execute("INSERT INTO job_offers(application_id,recipient_id,details,status,created_by) VALUES (?,?,'Fictional','accepted',?)", (app, candidate, hr)).lastrowid
    review = begin(client, app)
    preview = review['state']['rejection_preview']
    assert (preview['offer_id'], preview['offer_status']) == (offer, 'accepted')
    assert decide(client, review).status_code == 200
    with connect(path) as db:
        assert db.execute('SELECT status FROM job_offers WHERE id=?', (offer,)).fetchone()[0] == 'accepted'
        assert db.execute('SELECT status FROM applications WHERE id=?', (app,)).fetchone()[0] == 'rejected'


def test_pending_offer_is_shown_and_withdrawn(case):
    client, path, app, candidate, hr, _, _ = case
    with connect(path) as db:
        db.execute("UPDATE applications SET status='offered' WHERE id=?", (app,))
        offer = db.execute("INSERT INTO job_offers(application_id,recipient_id,details,status,created_by) VALUES (?,?,'Fictional','presented',?)", (app, candidate, hr)).lastrowid
    review = begin(client, app)
    preview = review['state']['rejection_preview']
    assert (preview['offer_id'], preview['offer_status']) == (offer, 'presented')
    assert decide(client, review).status_code == 200
    with connect(path) as db:
        assert db.execute('SELECT status FROM job_offers WHERE id=?', (offer,)).fetchone()[0] == 'withdrawn'


def test_delivery_created_after_review_cannot_be_bypassed(case):
    client, path, app, _, hr, interview, _ = case
    review = begin(client, app)
    with connect(path) as db:
        db.execute("""INSERT INTO google_deliveries(interview_id,application_id,kind,recipient,start_at,end_at,state,created_by)
            VALUES (?,?,'email','fictional@example.com','2099-01-01T09:00:00Z','2099-01-01T10:00:00Z','sending',?)""", (interview, app, hr))
    assert decide(client, review).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (app,)).fetchone()[0] == 'advanced'
        assert db.execute('SELECT status FROM interviews WHERE id=?', (interview,)).fetchone()[0] == 'scheduled'


def test_interview_start_crossing_rolls_back_rejection(case, monkeypatch):
    from datetime import UTC, datetime

    from app import graph_orchestrator

    client, path, app, _, _, interview, slot = case
    review = begin(client, app)
    original_record = graph_orchestrator.record_rejection

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2100, 1, 1, tzinfo=UTC)

    def cross_boundary(*args, **kwargs):
        original_record(*args, **kwargs)
        monkeypatch.setattr(graph_orchestrator, 'datetime', Later)

    monkeypatch.setattr(graph_orchestrator, 'record_rejection', cross_boundary)
    assert decide(client, review).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (app,)).fetchone()[0] == 'advanced'
        assert db.execute('SELECT status FROM interviews WHERE id=?', (interview,)).fetchone()[0] == 'scheduled'
        assert db.execute('SELECT withdrawn FROM interview_slots WHERE id=?', (slot,)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (app,)).fetchone()[0] == 0


def test_reason_and_authorization_are_required(case):
    client, path, app, _, _, _, _ = case
    assert client.post('/api/graph-reviews', json={'intent':'reject_application','application_id':app,'rejection_reason':'  '}).status_code == 422
    review = begin(client, app)
    assert client.post(f"/api/graph-reviews/{review['id']}/resume", json={'reviewed':True}).status_code == 409
    assert client.post('/api/auth/logout').status_code == 204
    assert client.post('/api/auth/login', json={'username':'candidate','password':'candidate-demo-only'}).status_code == 200
    assert decide(client, review).status_code == 403
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (app,)).fetchone()[0] == 'advanced'

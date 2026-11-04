"""Human-approved hiring is bound to the current accepted offer."""
import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.fixture
def case(tmp_path):
    path = tmp_path / 'hire.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            application = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','synthetic.pdf','fictional','offered')", (candidate,)).lastrowid
            offer = db.execute("INSERT INTO job_offers(application_id,recipient_id,details,status,created_by) VALUES (?,?,'Synthetic terms','accepted',?)", (application, candidate, hr)).lastrowid
        assert client.post('/api/auth/login', json={'username':'hr','password':'hr-demo-only'}).status_code == 200
        yield client, path, application, candidate, hr, offer


def begin(client, application):
    response = client.post('/api/graph-reviews', json={'intent':'finalize_hire', 'application_id':application})
    assert response.status_code == 201, response.text
    return response.json()


def decide(client, review, approved=True):
    payload = {'reviewed': approved}
    if approved:
        payload['approval_fingerprint'] = review['state']['action_proposal']['approval_fingerprint']
    return client.post(f"/api/graph-reviews/{review['id']}/resume", json=payload)


def test_hire_review_approves_once_and_withdraws_private_slots(case):
    client, path, app, candidate, hr, offer = case
    with connect(path) as db:
        slot = db.execute("INSERT INTO interview_slots(start_at,end_at,interviewer,interviewer_key,created_by,target_application_id,target_candidate_id) VALUES ('2099-01-01T09:00:00Z','2099-01-01T10:00:00Z','Synthetic','synthetic',?,?,?)", (hr, app, candidate)).lastrowid
    review = begin(client, app)
    preview = review['state']['hire_preview']
    assert (preview['candidate_id'], preview['offer_id'], preview['details']) == (candidate, offer, 'Synthetic terms')
    with connect(path) as db:
        for table in ('checkpoints', 'writes'):
            for row in db.execute(f'SELECT * FROM {table}'):
                assert all('Synthetic terms' not in str(value) for value in row)
    result = decide(client, review)
    assert result.status_code == 200, result.text
    assert result.json()['state']['action_result']['status'] == 'hired'
    assert decide(client, review).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (app,)).fetchone()[0] == 'hired'
        assert db.execute('SELECT withdrawn FROM interview_slots WHERE id=?', (slot,)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (app,)).fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM slot_audit_events WHERE slot_id=? AND action='slot_withdrawn'", (slot,)).fetchone()[0] == 1


@pytest.mark.parametrize('change', ['offer_reverted', 'owner_reverted', 'status_reverted', 'account_reverted', 'candidate_name_reverted', 'job_title_reverted'])
def test_changed_then_restored_inputs_reject_approval(case, change):
    client, path, app, candidate, hr, offer = case
    review = begin(client, app)
    with connect(path) as db:
        if change == 'offer_reverted':
            db.execute("UPDATE job_offers SET details='changed' WHERE id=?", (offer,))
            db.execute("UPDATE job_offers SET details='Synthetic terms' WHERE id=?", (offer,))
        elif change == 'owner_reverted':
            db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (hr, app))
            db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (candidate, app))
        elif change == 'status_reverted':
            db.execute("UPDATE applications SET status='advanced' WHERE id=?", (app,))
            db.execute("UPDATE applications SET status='offered' WHERE id=?", (app,))
        elif change == 'candidate_name_reverted':
            name = db.execute('SELECT display_name FROM users WHERE id=?', (candidate,)).fetchone()[0]
            db.execute("UPDATE users SET display_name='Changed' WHERE id=?", (candidate,))
            db.execute('UPDATE users SET display_name=? WHERE id=?', (name, candidate))
        elif change == 'job_title_reverted':
            title = db.execute("SELECT title FROM jobs WHERE id='job-backend-python'").fetchone()[0]
            db.execute("UPDATE jobs SET title='Changed' WHERE id='job-backend-python'")
            db.execute("UPDATE jobs SET title=? WHERE id='job-backend-python'", (title,))
        else:
            db.execute('UPDATE users SET active=0 WHERE id=?', (candidate,))
            db.execute('UPDATE users SET active=1 WHERE id=?', (candidate,))
    assert client.get(f"/api/graph-reviews/{review['id']}").json()['state']['hire_preview']['status'] == 'stale'
    assert decide(client, review).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (app,)).fetchone()[0] == 'offered'


def test_unaccepted_offer_and_active_interview_block_hire(case):
    client, path, app, candidate, hr, offer = case
    with connect(path) as db:
        db.execute("UPDATE job_offers SET status='presented' WHERE id=?", (offer,))
    assert client.post('/api/graph-reviews', json={'intent':'finalize_hire','application_id':app}).status_code == 409
    with connect(path) as db:
        db.execute("UPDATE job_offers SET status='accepted' WHERE id=?", (offer,))
        db.execute("INSERT INTO interviews(application_id,candidate_id,start_at,end_at,interviewer,interviewer_key,status,created_by) VALUES (?,?,'2099-01-01T09:00:00Z','2099-01-01T10:00:00Z','Synthetic','synthetic','scheduled',?)", (app, candidate, hr))
    assert client.post('/api/graph-reviews', json={'intent':'finalize_hire','application_id':app}).status_code == 409


def test_decline_does_not_hire(case):
    client, path, app, _, _, _ = case
    review = begin(client, app)
    assert decide(client, review, False).json()['state']['action_result']['status'] == 'declined'
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (app,)).fetchone()[0] == 'offered'


def test_hire_rejects_missing_or_wrong_approval_and_non_hr(case):
    client, path, app, _, _, _ = case
    review = begin(client, app)
    endpoint = f"/api/graph-reviews/{review['id']}/resume"
    assert client.post(endpoint, json={'reviewed': True}).status_code == 409
    assert client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': '0' * 64}).status_code == 409
    assert client.post('/api/auth/logout').status_code == 204
    assert client.post('/api/auth/login', json={'username':'candidate','password':'candidate-demo-only'}).status_code == 200
    assert client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': review['state']['action_proposal']['approval_fingerprint']}).status_code == 403
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (app,)).fetchone()[0] == 'offered'


def test_candidate_acceptance_then_graph_hire(case):
    client, path, app, candidate, _, offer = case
    with connect(path) as db:
        db.execute("UPDATE job_offers SET status='presented' WHERE id=?", (offer,))
    assert client.post('/api/auth/logout').status_code == 204
    assert client.post('/api/auth/login', json={'username':'candidate','password':'candidate-demo-only'}).status_code == 200
    assert client.post(f'/api/offers/{offer}/respond', json={'decision':'accepted'}).status_code == 200
    assert client.post('/api/auth/logout').status_code == 204
    assert client.post('/api/auth/login', json={'username':'hr','password':'hr-demo-only'}).status_code == 200
    review = begin(client, app)
    assert review['state']['hire_preview']['candidate_id'] == candidate
    assert decide(client, review).status_code == 200
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (app,)).fetchone()[0] == 'hired'


@pytest.mark.parametrize('field,value', [('active', 0), ('role', 'employee')])
def test_ineligible_candidate_cannot_be_hired(case, field, value):
    client, path, app, candidate, _, _ = case
    with connect(path) as db:
        db.execute(f'UPDATE users SET {field}=? WHERE id=?', (value, candidate))
    assert client.post('/api/graph-reviews', json={'intent':'finalize_hire', 'application_id':app}).status_code == 409
    assert client.post(f'/api/applications/{app}/decision', json={'decision':'hire'}).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (app,)).fetchone()[0] == 'offered'


def test_stale_hire_review_can_be_discarded(case):
    client, path, app, _, _, offer = case
    review = begin(client, app)
    with connect(path) as db:
        db.execute("UPDATE job_offers SET details='Changed terms' WHERE id=?", (offer,))
    assert client.get(f"/api/graph-reviews/{review['id']}").json()['state']['hire_preview']['status'] == 'stale'
    assert decide(client, review).status_code == 409
    assert decide(client, review, False).json()['state']['action_result']['status'] == 'declined'
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (app,)).fetchone()[0] == 'offered'

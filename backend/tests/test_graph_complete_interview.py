"""HR-approved graph completion uses the same transactional interview operation."""
import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.fixture
def case(tmp_path):
    path = tmp_path / 'graph-completion.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            application = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','test.pdf','private-resume-marker','advanced')", (candidate,)).lastrowid
            interview = db.execute("""INSERT INTO interviews(application_id,candidate_id,start_at,end_at,interviewer,interviewer_key,status,created_by)
                VALUES (?,?,'2020-01-01T10:00:00Z','2020-01-01T11:00:00Z','Private Interviewer','private interviewer','scheduled',?)""", (application, candidate, hr)).lastrowid
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        yield client, path, application, interview


def begin(client, application, interview):
    result = client.post('/api/graph-reviews', json={'intent':'complete_interview','application_id':application,'interview_id':interview})
    assert result.status_code == 201, result.text
    data = result.json()
    assert data['awaiting_review']
    assert data['state']['interview_preview']['status'] == 'current'
    return data


def test_approval_records_once_without_private_checkpoint(case):
    client, path, application, interview = case
    review = begin(client, application, interview)
    assert review['state']['interview_preview']['interviewer'] == 'Private Interviewer'
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM interview_completions').fetchone()[0] == 0
        for table in ('checkpoints', 'writes'):
            for row in db.execute(f'SELECT * FROM {table}'):
                assert all('Private Interviewer' not in str(value) and 'private-resume-marker' not in str(value) for value in row)
    endpoint = f"/api/graph-reviews/{review['id']}/resume"
    assert client.post(endpoint, json={'reviewed': True}).status_code == 409
    response = client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': review['state']['action_proposal']['approval_fingerprint']})
    assert response.status_code == 200, response.text
    assert response.json()['state']['action_result'] == {'status':'interview_completed','interview_id':interview}
    assert client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': review['state']['action_proposal']['approval_fingerprint']}).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM interview_completions').fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='interview_completed' AND application_id=?", (application,)).fetchone()[0] == 1
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'advanced'


def test_decline_and_changes_do_not_record_completion(case):
    client, path, application, interview = case
    declined = begin(client, application, interview)
    result = client.post(f"/api/graph-reviews/{declined['id']}/resume", json={'reviewed':False})
    assert result.status_code == 200 and result.json()['state']['action_result']['status'] == 'declined'
    stale = begin(client, application, interview)
    with connect(path) as db:
        db.execute("UPDATE interviews SET interviewer='Changed' WHERE id=?", (interview,))
        db.execute("UPDATE interviews SET interviewer='Private Interviewer' WHERE id=?", (interview,))
    assert client.get(f"/api/graph-reviews/{stale['id']}").json()['state']['interview_preview']['status'] == 'stale'
    response = client.post(f"/api/graph-reviews/{stale['id']}/resume", json={'reviewed':True,'approval_fingerprint':stale['state']['action_proposal']['approval_fingerprint']})
    assert response.status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM interview_completions').fetchone()[0] == 0


def test_owner_role_and_end_time_are_enforced(case):
    client, path, application, interview = case
    assert client.post('/api/graph-reviews', json={'intent':'complete_interview','application_id':application,'interview_id':interview+10}).status_code == 404
    with connect(path) as db:
        db.execute("UPDATE interviews SET end_at='2999-01-01T11:00:00Z' WHERE id=?", (interview,))
    assert client.post('/api/graph-reviews', json={'intent':'complete_interview','application_id':application,'interview_id':interview}).status_code == 409
    assert client.post('/api/auth/login', json={'username':'candidate','password':'candidate-demo-only'}).status_code == 200
    assert client.post('/api/graph-reviews', json={'intent':'complete_interview','application_id':application,'interview_id':interview}).status_code == 403


def test_concurrent_approvals_record_exactly_once(case):
    from concurrent.futures import ThreadPoolExecutor

    client, path, application, interview = case
    review = begin(client, application, interview)
    endpoint = f"/api/graph-reviews/{review['id']}/resume"
    body = {'reviewed': True, 'approval_fingerprint': review['state']['action_proposal']['approval_fingerprint']}
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: client.post(endpoint, json=body).status_code, range(2)))
    assert sorted(responses) == [200, 409]
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM interview_completions').fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='interview_completed'").fetchone()[0] == 1


def test_revoked_session_and_reassigned_owner_cannot_approve(case):
    client, path, application, interview = case
    review = begin(client, application, interview)
    with connect(path) as db:
        actor = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
        db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (actor, application))
    response = client.post(f"/api/graph-reviews/{review['id']}/resume", json={'reviewed':True,'approval_fingerprint':review['state']['action_proposal']['approval_fingerprint']})
    assert response.status_code == 409
    with connect(path) as db:
        candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
        db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (candidate, application))
    review = begin(client, application, interview)
    assert client.post('/api/auth/logout').status_code == 204
    assert client.post(f"/api/graph-reviews/{review['id']}/resume", json={'reviewed':True,'approval_fingerprint':review['state']['action_proposal']['approval_fingerprint']}).status_code == 401
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM interview_completions').fetchone()[0] == 0

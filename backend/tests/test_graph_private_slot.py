from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import graph_orchestrator as graph
from app.auth import hash_password
from app.db import connect
from app.main import create_app


@pytest.fixture
def scenario(tmp_path):
    path = tmp_path / 'private-graph.sqlite3'
    with TestClient(create_app(path, seed_demo=True), raise_server_exceptions=False) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            owner = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            other = db.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES ('other','Other','candidate',?)",
                               (hash_password('other-password'),)).lastrowid
            first = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?, 'job-backend-python', 'a.pdf', 'synthetic', 'advanced')", (owner,)).lastrowid
            second = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?, 'job-backend-python', 'b.pdf', 'synthetic', 'advanced')", (other,)).lastrowid
        yield path, client, first, second


def start(client, application_id):
    time = datetime.now(UTC) + timedelta(days=3)
    response = client.post('/api/graph-reviews', json={
        'application_id': application_id, 'intent': 'create_private_slot',
        'slot': {'start_at': time.isoformat(), 'end_at': (time + timedelta(minutes=30)).isoformat(),
                 'interviewer': 'Interviewer'},
    })
    assert response.status_code == 201, response.text
    return response.json()


def resume(client, review, reviewed=True):
    payload = {'reviewed': reviewed}
    if reviewed:
        payload['approval_fingerprint'] = review['state']['action_proposal']['approval_fingerprint']
    return client.post(f"/api/graph-reviews/{review['id']}/resume", json=payload)


def test_private_slot_requires_review_and_is_visible_only_to_target(scenario):
    path, client, application_id, other_application = scenario
    review = start(client, application_id)
    assert review['awaiting_review']
    assert review['state']['action_proposal']['type'] == 'create_private_slot'
    assert review['state']['action_proposal']['availability_scope'] == 'selected_application_only'
    assert 'no invitation is sent' in review['state']['action_proposal']['notice']
    assert client.post(f"/api/graph-reviews/{review['id']}/resume", json={'reviewed': True}).status_code == 409
    result = resume(client, review)
    assert result.status_code == 200, result.text
    slot_id = result.json()['state']['action_result']['slot']['id']
    assert resume(client, review).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM interviews').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM slot_audit_events').fetchone()[0] == 1
    assert client.post('/api/auth/login', json={'username': 'other', 'password': 'other-password'}).status_code == 200
    assert client.get('/api/slots', params={'application_id': other_application}).json() == []
    assert client.post(f'/api/slots/{slot_id}/book', json={'application_id': other_application}).status_code == 404
    assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
    assert [slot['id'] for slot in client.get('/api/slots', params={'application_id': application_id}).json()] == [slot_id]
    assert client.post(f'/api/slots/{slot_id}/book', json={'application_id': application_id}).status_code == 201


def test_decline_and_conflict_do_not_create_private_slot(scenario):
    path, client, application_id, _ = scenario
    declined = start(client, application_id)
    assert resume(client, declined, reviewed=False).json()['state']['action_result'] == {'status': 'declined'}
    review = start(client, application_id)
    assert client.post('/api/slots', json=review['state']['action_proposal']['slot']).status_code == 201
    assert resume(client, review).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM interview_slots WHERE target_application_id IS NOT NULL').fetchone()[0] == 0


def test_private_slot_rolls_back_on_failed_creation(scenario, monkeypatch):
    path, client, application_id, _ = scenario
    review = start(client, application_id)
    original = graph.create_slot_record

    def fail(*args):
        original(*args)
        raise RuntimeError('simulated failure')

    with monkeypatch.context() as patch:
        patch.setattr(graph, 'create_slot_record', fail)
        assert resume(client, review).status_code == 500
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM slot_audit_events').fetchone()[0] == 0
    assert resume(client, review).status_code == 200


def test_changed_candidate_invalidates_approval(scenario):
    path, client, application_id, other_application = scenario
    review = start(client, application_id)
    with connect(path) as db:
        other = db.execute('SELECT candidate_id FROM applications WHERE id=?', (other_application,)).fetchone()[0]
        db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (other, application_id))
    assert resume(client, review).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0

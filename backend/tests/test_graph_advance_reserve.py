"""A combined decision and private reservation share one approval and transaction."""
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.fixture
def scenario(tmp_path):
    app = create_app(tmp_path / 'combined.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(app.state.database_path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','cv.pdf','Python SQL','submitted')", (candidate,)).lastrowid
        criterion = client.post('/api/jobs/job-backend-python/rubric/criteria', json={'category': 'mandatory', 'label': 'Python'}).json()['criterion_ref']
        evidence = client.post(f'/api/applications/{application}/rubric/evidence', json={'start': 0, 'end': 6}).json()['evidence_ref']
        response = client.post(f'/api/applications/{application}/rubric/assessment', json={'judgments': [
            {'criterion_ref': criterion, 'status': 'met', 'evidence_refs': [evidence]},
        ]})
        assert response.status_code == 201
        start = datetime.now(UTC) + timedelta(days=7)
        slot = {'start_at': start.isoformat(), 'end_at': (start + timedelta(minutes=30)).isoformat(), 'interviewer': 'Synthetic Reviewer'}
        yield client, app.state.database_path, application, candidate, slot


def start_review(client, application, slot):
    response = client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'advance_and_reserve_slot', 'slot': slot})
    assert response.status_code == 201, response.text
    result = response.json()
    assert result['awaiting_review'] and result['state']['matching_specialist']['status'] == 'current'
    proposal = result['state']['action_proposal']
    assert proposal['type'] == 'advance_and_reserve_slot'
    assert proposal['availability_scope'] == 'selected_application_only'
    return result['id'], proposal['approval_fingerprint']


def test_combined_approval_is_atomic_private_and_replay_safe(scenario):
    client, path, application, candidate, slot = scenario
    review, fingerprint = start_review(client, application, slot)
    endpoint = f'/api/graph-reviews/{review}/resume'
    assert client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': 'bad'}).status_code == 409
    result = client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': fingerprint})
    assert result.status_code == 200, result.text
    assert result.json()['state']['action_result']['status'] == 'advanced_and_reserved'
    slot_id = result.json()['state']['action_result']['slot']['id']
    assert client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': fingerprint}).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'advanced'
        rows = db.execute('SELECT target_application_id,target_candidate_id FROM interview_slots').fetchall()
        assert len(rows) == 1 and tuple(rows[0]) == (application, candidate)
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (application,)).fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM interviews').fetchone()[0] == 0
    assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
    assert client.post(f'/api/slots/{slot_id}/book', json={'application_id': application}).status_code == 201


def test_conflicting_slot_rolls_back_advancement_and_can_be_declined(scenario):
    client, path, application, _, slot = scenario
    review, fingerprint = start_review(client, application, slot)
    response = client.post('/api/slots', json=slot)
    assert response.status_code == 201, response.text
    endpoint = f'/api/graph-reviews/{review}/resume'
    assert client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': fingerprint}).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (application,)).fetchone()[0] == 0
    declined = client.post(endpoint, json={'reviewed': False})
    assert declined.status_code == 200 and declined.json()['state']['action_result']['status'] == 'declined'


def test_changed_assessment_and_candidate_cannot_approve(scenario):
    client, path, application, _, slot = scenario
    review, fingerprint = start_review(client, application, slot)
    assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
    assert client.post(f'/api/graph-reviews/{review}/resume', json={'reviewed': True, 'approval_fingerprint': fingerprint}).status_code == 403
    assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
    with connect(path) as db:
        db.execute("UPDATE rubric_evidence SET excerpt='changed' WHERE application_id=?", (application,))
    assert client.post(f'/api/graph-reviews/{review}/resume', json={'reviewed': True, 'approval_fingerprint': fingerprint}).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0


def test_changed_then_restored_evidence_cannot_reuse_approval(scenario):
    client, path, application, _, slot = scenario
    review, fingerprint = start_review(client, application, slot)
    with connect(path) as db:
        db.execute("UPDATE rubric_evidence SET excerpt='changed' WHERE application_id=?", (application,))
        db.execute("UPDATE rubric_evidence SET excerpt='Python' WHERE application_id=?", (application,))
    assert client.post(f'/api/graph-reviews/{review}/resume', json={'reviewed': True, 'approval_fingerprint': fingerprint}).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0


def test_changed_candidate_cannot_reuse_approval(scenario):
    client, path, application, _, slot = scenario
    review, fingerprint = start_review(client, application, slot)
    with connect(path) as db:
        other = db.execute("SELECT id FROM users WHERE username='employee'").fetchone()[0]
        db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (other, application))
    assert client.post(f'/api/graph-reviews/{review}/resume', json={'reviewed': True, 'approval_fingerprint': fingerprint}).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0


def test_expired_authorization_rolls_back_both_writes_and_checkpoint(scenario, monkeypatch):
    from app import graph_orchestrator

    client, path, application, _, slot = scenario
    review, fingerprint = start_review(client, application, slot)
    original = graph_orchestrator._authorize_resume
    calls = 0

    def authorize(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(status_code=401, detail='Session expired')
        return original(*args)

    monkeypatch.setattr(graph_orchestrator, '_authorize_resume', authorize)
    endpoint = f'/api/graph-reviews/{review}/resume'
    assert client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': fingerprint}).status_code == 401
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (application,)).fetchone()[0] == 0
    monkeypatch.setattr(graph_orchestrator, '_authorize_resume', original)
    assert client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': fingerprint}).status_code == 200

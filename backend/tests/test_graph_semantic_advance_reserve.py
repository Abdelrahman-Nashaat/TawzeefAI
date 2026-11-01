"""The same reviewed action includes current semantic evidence and private scheduling."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.fixture
def case(tmp_path, monkeypatch):
    from app import semantic_screening

    monkeypatch.setenv('TAWZEEF_ENABLE_SEMANTIC_SCREENING', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'local')

    class Model:
        def encode(self, texts, *, query):
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(semantic_screening, '_model', lambda _: Model())
    app = create_app(tmp_path / 'semantic-advance.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(app.state.database_path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','cv.pdf','Python SQL','submitted')", (candidate,)).lastrowid
        criterion = client.post('/api/jobs/job-backend-python/rubric/criteria', json={'category': 'mandatory', 'label': 'Python'}).json()['criterion_ref']
        evidence = client.post(f'/api/applications/{application}/rubric/evidence', json={'start': 0, 'end': 6}).json()['evidence_ref']
        assert client.post(f'/api/applications/{application}/rubric/assessment', json={'judgments': [
            {'criterion_ref': criterion, 'status': 'met', 'evidence_refs': [evidence]},
        ]}).status_code == 201
        start = datetime.now(UTC) + timedelta(days=7)
        slot = {'start_at': start.isoformat(), 'end_at': (start + timedelta(minutes=30)).isoformat(), 'interviewer': 'Synthetic Reviewer'}
        yield client, app.state.database_path, application, candidate, slot


def start_review(client, application, slot):
    response = client.post('/api/graph-reviews', json={
        'application_id': application, 'intent': 'semantic_advance_and_reserve_slot', 'slot': slot})
    assert response.status_code == 201, response.text
    review = response.json()
    assert review['awaiting_review']
    assert review['state']['semantic_suggestions']['status'] == 'current'
    assert review['state']['matching_specialist']['status'] == 'current'
    proposal = review['state']['action_proposal']
    assert proposal['type'] == 'advance_and_reserve_slot'
    assert proposal['availability_scope'] == 'selected_application_only'
    return review['id'], proposal['approval_fingerprint']


def test_semantic_review_advances_and_reserves_with_private_checkpoint(case):
    client, path, application, candidate, slot = case
    review, fingerprint = start_review(client, application, slot)
    with sqlite3.connect(path) as db:
        checkpoints = db.execute('SELECT checkpoint FROM checkpoints').fetchall()
        assert checkpoints and all(b'Python SQL' not in row[0] for row in checkpoints)
    response = client.post(f'/api/graph-reviews/{review}/resume', json={'reviewed': True, 'approval_fingerprint': fingerprint})
    assert response.status_code == 200, response.text
    assert response.json()['state']['action_result']['status'] == 'advanced_and_reserved'
    slot_id = response.json()['state']['action_result']['slot']['id']
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'advanced'
        assert tuple(db.execute('SELECT target_application_id,target_candidate_id FROM interview_slots WHERE id=?', (slot_id,)).fetchone()) == (application, candidate)
    assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
    assert client.get(f'/api/graph-reviews/{review}').status_code == 403
    assert client.post(f'/api/slots/{slot_id}/book', json={'application_id': application}).status_code == 201


@pytest.mark.parametrize('change', ['resume', 'criteria_reverted', 'owner_reverted'])
def test_stale_semantic_approval_never_advances(case, change):
    client, path, application, candidate, slot = case
    review, fingerprint = start_review(client, application, slot)
    with connect(path) as db:
        if change == 'resume':
            db.execute("UPDATE applications SET resume_text='Changed' WHERE id=?", (application,))
        elif change == 'criteria_reverted':
            db.execute("UPDATE rubric_criteria SET label='Changed' WHERE job_id='job-backend-python'")
            db.execute("UPDATE rubric_criteria SET label='Python' WHERE job_id='job-backend-python'")
        else:
            other = db.execute("SELECT id FROM users WHERE username='employee'").fetchone()[0]
            db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (other, application))
            db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (candidate, application))
    assert client.get(f'/api/graph-reviews/{review}').json()['state']['semantic_suggestions']['status'] == 'stale'
    assert client.post(f'/api/graph-reviews/{review}/resume', json={'reviewed': True, 'approval_fingerprint': fingerprint}).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0


def test_conflict_rolls_back_both_actions(case):
    client, path, application, _, slot = case
    review, fingerprint = start_review(client, application, slot)
    assert client.post('/api/slots', json=slot).status_code == 201
    response = client.post(f'/api/graph-reviews/{review}/resume', json={'reviewed': True, 'approval_fingerprint': fingerprint})
    assert response.status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (application,)).fetchone()[0] == 0


def test_expired_session_after_writes_rolls_back_both_and_checkpoint(case, monkeypatch):
    from app import graph_orchestrator

    client, path, application, _, slot = case
    review, fingerprint = start_review(client, application, slot)
    original = graph_orchestrator._authorize_resume
    calls = 0

    def expire_after_write(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(status_code=401, detail='Session expired')
        return original(*args)

    monkeypatch.setattr(graph_orchestrator, '_authorize_resume', expire_after_write)
    endpoint = f'/api/graph-reviews/{review}/resume'
    assert client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': fingerprint}).status_code == 401
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (application,)).fetchone()[0] == 0
    monkeypatch.setattr(graph_orchestrator, '_authorize_resume', original)
    response = client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': fingerprint})
    assert response.status_code == 200, response.text

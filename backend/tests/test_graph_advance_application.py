"""HR-approved graph transition from reviewed application to interview stage."""

import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.fixture
def case(tmp_path):
    app = create_app(tmp_path / 'advance.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(app.state.database_path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','synthetic.pdf','Python SQL','submitted')", (candidate,)).lastrowid
        criterion = client.post('/api/jobs/job-backend-python/rubric/criteria', json={'category': 'mandatory', 'label': 'Python'}).json()['criterion_ref']
        evidence = client.post(f'/api/applications/{application}/rubric/evidence', json={'start': 0, 'end': 6}).json()['evidence_ref']
        response = client.post(f'/api/applications/{application}/rubric/assessment', json={'judgments': [
            {'criterion_ref': criterion, 'status': 'met', 'evidence_refs': [evidence]},
        ]})
        assert response.status_code == 201
        yield client, app.state.database_path, application


def start(client, application):
    response = client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'advance_application'})
    assert response.status_code == 201, response.text
    data = response.json()
    assert data['awaiting_review']
    assert data['state']['matching_specialist']['status'] == 'current'
    proposal = data['state']['action_proposal']
    assert proposal['type'] == 'advance_application' and proposal['application_id'] == application
    assert proposal['current_status'] == 'submitted' and proposal['target_status'] == 'advanced'
    return data['id'], proposal['approval_fingerprint']


def test_approval_advances_once_and_is_audited(case):
    client, path, application = case
    review, fingerprint = start(client, application)
    endpoint = f'/api/graph-reviews/{review}/resume'
    assert client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': 'bad'}).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
    response = client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': fingerprint})
    assert response.status_code == 200, response.text
    assert response.json()['state']['action_result'] == {'status': 'advanced'}
    assert client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': fingerprint}).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'advanced'
        events = db.execute("SELECT details FROM audit_events WHERE application_id=? AND action='decision'", (application,)).fetchall()
        assert len(events) == 1 and json.loads(events[0][0]) == {'decision': 'advance', 'note': ''}
        assert db.execute('SELECT count(*) FROM interviews WHERE application_id=?', (application,)).fetchone()[0] == 0


@pytest.mark.parametrize('mutation', ['resume', 'criteria', 'evidence', 'assessment', 'status'])
def test_changed_inputs_never_advance(case, mutation):
    client, path, application = case
    review, fingerprint = start(client, application)
    with connect(path) as db:
        if mutation == 'resume':
            db.execute("UPDATE applications SET resume_text='Changed' WHERE id=?", (application,))
        elif mutation == 'criteria':
            db.execute("UPDATE rubric_criteria SET label='Changed' WHERE job_id='job-backend-python'")
        elif mutation == 'evidence':
            db.execute("UPDATE rubric_evidence SET excerpt='Changed' WHERE application_id=?", (application,))
        elif mutation == 'assessment':
            db.execute("UPDATE rubric_assessments SET assessment_json='{}' WHERE application_id=?", (application,))
        else:
            db.execute("UPDATE applications SET status='rejected' WHERE id=?", (application,))
    response = client.post(f'/api/graph-reviews/{review}/resume', json={'reviewed': True, 'approval_fingerprint': fingerprint})
    assert response.status_code == 409, response.text
    with connect(path) as db:
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (application,)).fetchone()[0] == 0
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] != 'advanced'


def test_decline_and_access_controls(case):
    client, path, application = case
    review, fingerprint = start(client, application)
    assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
    assert client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'advance_application'}).status_code == 403
    assert client.post(f'/api/graph-reviews/{review}/resume', json={'reviewed': True, 'approval_fingerprint': fingerprint}).status_code == 403
    assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
    result = client.post(f'/api/graph-reviews/{review}/resume', json={'reviewed': False})
    assert result.status_code == 200 and result.json()['state']['action_result'] == {'status': 'declined'}
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'


def test_requires_current_reviewed_assessment(case):
    client, path, application = case
    with connect(path) as db:
        db.execute('DELETE FROM rubric_assessments WHERE application_id=?', (application,))
    response = client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'advance_application'})
    assert response.status_code == 409


def test_restored_evidence_does_not_revive_old_approval(case):
    client, path, application = case
    review, fingerprint = start(client, application)
    with connect(path) as db:
        db.execute("UPDATE rubric_evidence SET excerpt='Changed' WHERE application_id=?", (application,))
        db.execute("UPDATE rubric_evidence SET excerpt='Python' WHERE application_id=?", (application,))
    response = client.post(f'/api/graph-reviews/{review}/resume',
                           json={'reviewed': True, 'approval_fingerprint': fingerprint})
    assert response.status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'


def test_mask_change_and_revert_do_not_revive_advance_approval(case):
    client, path, application = case
    review, fingerprint = start(client, application)
    endpoint = f'/api/applications/{application}/resume/mask'
    for spans in ([{'start': 0, 'end': 6}], []):
        view = client.get(endpoint).json()
        response = client.put(endpoint, json={
            'source_sha256': view['source_sha256'], 'expected_source_revision': view['source_revision'],
            'expected_revision': view['revision'], 'spans': spans,
        })
        assert response.status_code == 200
    response = client.post(f'/api/graph-reviews/{review}/resume',
                           json={'reviewed': True, 'approval_fingerprint': fingerprint})
    assert response.status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (application,)).fetchone()[0] == 0


def test_auth_revoked_before_commit_rolls_back_transition(case, monkeypatch):
    from app import graph_orchestrator

    client, path, application = case
    review, fingerprint = start(client, application)
    original = graph_orchestrator._authorize_resume
    calls = 0

    def authorize(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(status_code=401, detail='Session expired')
        return original(*args)

    monkeypatch.setattr(graph_orchestrator, '_authorize_resume', authorize)
    response = client.post(f'/api/graph-reviews/{review}/resume',
                           json={'reviewed': True, 'approval_fingerprint': fingerprint})
    assert response.status_code == 401
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (application,)).fetchone()[0] == 0
    assert client.get(f'/api/graph-reviews/{review}').json()['awaiting_review']

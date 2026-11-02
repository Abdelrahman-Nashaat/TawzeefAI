"""A screening is recorded only after an authorized, current HR review."""
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.fixture
def case(tmp_path):
    app = create_app(tmp_path / 'screen.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(app.state.database_path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','synthetic.pdf','Python private-marker-xqz123','submitted')", (candidate,)).lastrowid
        yield client, app.state.database_path, application


def start(client, application):
    response = client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'screen_application'})
    assert response.status_code == 201, response.text
    data = response.json()
    assert data['awaiting_review']
    assert data['state']['plan'] == ['parser', 'matching', 'status', 'prepare_screen', 'human_review', 'execute_screen']
    proposal = data['state']['action_proposal']
    assert proposal['type'] == 'screen_application' and proposal['application_id'] == application
    assert proposal['current_status'] == 'submitted'
    assert data['state']['screening_preview']['status'] == 'current'
    return data


def counts(db, application):
    return tuple(db.execute(sql, (application,)).fetchone()[0] for sql in (
        "SELECT count(*) FROM audit_events WHERE application_id=? AND action='screened'",
        "SELECT count(*) FROM screening_history h JOIN audit_events e ON e.id=h.audit_event_id WHERE e.application_id=?",
        "SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'"))


def test_approval_stores_exact_preview_once_without_checkpointing_evidence(case):
    client, path, application = case
    data = start(client, application)
    preview = data['state']['screening_preview']['score']
    review, fingerprint = data['id'], data['state']['action_proposal']['approval_fingerprint']
    assert 'private-marker-xqz123' in json.dumps(preview)
    with connect(path) as db:
        assert db.execute('SELECT score_json FROM applications WHERE id=?', (application,)).fetchone()[0] is None
        assert counts(db, application) == (0, 0, 0)
        for table in ('checkpoints', 'writes'):
            for row in db.execute(f'SELECT * FROM {table}'):
                for value in row:
                    if isinstance(value, str):
                        assert 'private-marker-xqz123' not in value
                    if isinstance(value, bytes):
                        assert b'private-marker-xqz123' not in value
    endpoint = f'/api/graph-reviews/{review}/resume'
    assert client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': 'bad'}).status_code == 409
    response = client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': fingerprint})
    assert response.status_code == 200, response.text
    assert response.json()['state']['action_result'] == {'status': 'screened'}
    assert client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': fingerprint}).status_code == 409
    with connect(path) as db:
        assert json.loads(db.execute('SELECT score_json FROM applications WHERE id=?', (application,)).fetchone()[0]) == preview
        assert counts(db, application) == (1, 1, 0)


def test_mask_changes_and_reverts_stale_pending_screening(case):
    client, path, application = case
    endpoint = f'/api/applications/{application}/resume/mask'

    def save(spans):
        current = client.get(endpoint).json()
        response = client.put(endpoint, json={
            'source_sha256': current['source_sha256'], 'expected_source_revision': current['source_revision'],
            'expected_revision': current['revision'], 'spans': spans,
        })
        assert response.status_code == 200, response.text

    original = start(client, application)
    selection = [{'start': 0, 'end': len('Python')}]
    save(selection)
    original_state = client.get(f"/api/graph-reviews/{original['id']}").json()['state']
    assert original_state['screening_preview']['status'] == 'stale'
    masked = start(client, application)
    assert masked['state']['screening_preview']['score']['score'] == 0
    assert 'private-marker-xqz123' not in json.dumps(masked['state']['screening_preview']['score'])
    save([])
    save(selection)
    assert client.get(f"/api/graph-reviews/{masked['id']}").json()['state']['screening_preview']['status'] == 'stale'
    response = client.post(f"/api/graph-reviews/{masked['id']}/resume", json={
        'reviewed': True, 'approval_fingerprint': masked['state']['action_proposal']['approval_fingerprint']})
    assert response.status_code == 409
    with connect(path) as db:
        assert counts(db, application) == (0, 0, 0)


@pytest.mark.parametrize('mutation', ['resume', 'restored_resume', 'job', 'restored_job', 'candidate', 'status', 'restored_status', 'screened', 'restored_screened'])
def test_changed_inputs_invalidate_review(case, mutation):
    client, path, application = case
    data = start(client, application)
    with connect(path) as db:
        if mutation in ('resume', 'restored_resume'):
            original = db.execute('SELECT resume_text FROM applications WHERE id=?', (application,)).fetchone()[0]
            db.execute("UPDATE applications SET resume_text='changed' WHERE id=?", (application,))
            if mutation == 'restored_resume':
                db.execute('UPDATE applications SET resume_text=? WHERE id=?', (original, application))
        elif mutation in ('job', 'restored_job'):
            original = db.execute("SELECT title FROM jobs WHERE id='job-backend-python'").fetchone()[0]
            db.execute("UPDATE jobs SET title='changed' WHERE id='job-backend-python'")
            if mutation == 'restored_job':
                db.execute("UPDATE jobs SET title=? WHERE id='job-backend-python'", (original,))
        elif mutation == 'candidate':
            owner = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (owner, application))
        elif mutation in ('status', 'restored_status'):
            db.execute("UPDATE applications SET status='rejected' WHERE id=?", (application,))
            if mutation == 'restored_status':
                db.execute("UPDATE applications SET status='submitted' WHERE id=?", (application,))
        else:
            db.execute("UPDATE applications SET score_json='{}' WHERE id=?", (application,))
            if mutation == 'restored_screened':
                db.execute('UPDATE applications SET score_json=NULL WHERE id=?', (application,))
    response = client.get(f"/api/graph-reviews/{data['id']}")
    assert response.status_code == 200
    assert response.json()['state']['screening_preview']['status'] == 'stale'
    response = client.post(f"/api/graph-reviews/{data['id']}/resume", json={
        'reviewed': True, 'approval_fingerprint': data['state']['action_proposal']['approval_fingerprint']})
    assert response.status_code == 409, response.text
    with connect(path) as db:
        assert counts(db, application) == (0, 0, 0)


def test_decline_and_access_control(case):
    client, path, application = case
    data = start(client, application)
    assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
    assert client.get(f"/api/graph-reviews/{data['id']}").status_code == 403
    assert client.post(f"/api/graph-reviews/{data['id']}/resume", json={'reviewed': True}).status_code == 403
    assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
    response = client.post(f"/api/graph-reviews/{data['id']}/resume", json={'reviewed': False})
    assert response.status_code == 200 and response.json()['state']['action_result'] == {'status': 'declined'}
    with connect(path) as db:
        assert counts(db, application) == (0, 0, 0)


def test_screening_algorithm_change_invalidates_approval(case, monkeypatch):
    from app import graph_orchestrator

    client, path, application = case
    data = start(client, application)
    original = graph_orchestrator.score_masked_resume

    def changed_score(job, text):
        return {**original(job, text), 'score': 99}

    monkeypatch.setattr(graph_orchestrator, 'score_masked_resume', changed_score)
    assert client.get(f"/api/graph-reviews/{data['id']}").json()['state']['screening_preview']['status'] == 'stale'
    response = client.post(f"/api/graph-reviews/{data['id']}/resume", json={
        'reviewed': True, 'approval_fingerprint': data['state']['action_proposal']['approval_fingerprint']})
    assert response.status_code == 409
    with connect(path) as db:
        assert counts(db, application) == (0, 0, 0)


@pytest.mark.parametrize('change', ['terminal', 'screened'])
def test_requires_unscreened_submitted_application(case, change):
    client, path, application = case
    with connect(path) as db:
        if change == 'terminal':
            db.execute("UPDATE applications SET status='rejected' WHERE id=?", (application,))
        else:
            db.execute("UPDATE applications SET score_json='{}' WHERE id=?", (application,))
    response = client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'screen_application'})
    assert response.status_code == 409


def test_expired_authorization_rolls_back_data_and_checkpoint(case, monkeypatch):
    from app import graph_orchestrator

    client, path, application = case
    data = start(client, application)
    original = graph_orchestrator._authorize_resume
    calls = 0

    def authorize(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(status_code=401, detail='Session expired')
        return original(*args)

    monkeypatch.setattr(graph_orchestrator, '_authorize_resume', authorize)
    endpoint = f"/api/graph-reviews/{data['id']}/resume"
    payload = {'reviewed': True, 'approval_fingerprint': data['state']['action_proposal']['approval_fingerprint']}
    assert client.post(endpoint, json=payload).status_code == 401
    with connect(path) as db:
        assert counts(db, application) == (0, 0, 0)
        assert db.execute('SELECT score_json FROM applications WHERE id=?', (application,)).fetchone()[0] is None
    assert client.get(f"/api/graph-reviews/{data['id']}").json()['awaiting_review']
    monkeypatch.setattr(graph_orchestrator, '_authorize_resume', original)
    assert client.post(endpoint, json=payload).status_code == 200

"""Recruitment audit browsing is bounded, private, and role restricted."""

import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.fixture
def example(tmp_path):
    app = create_app(tmp_path / 'audit.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        with connect(app.state.database_path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            actor = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            applications = [db.execute('''INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?,?,'cv.docx','PRIVATE RESUME MARKER')''', (candidate, job)).lastrowid
                for job in ('job-backend-python', 'job-frontend-react')]
            entries = [
                (applications[0], 'screened', {'method': 'keyword_mentions_v1', 'score': 75,
                  'required_mentioned': 3, 'required_total': 4, 'evidence_status': 'limited',
                  'evidence': 'PRIVATE EVIDENCE MARKER'}),
                (applications[1], 'decision', {'decision': 'reject', 'note': 'PRIVATE NOTE MARKER'}),
                (applications[0], 'decision', {'decision': 'advance', 'note': 'PRIVATE NOTE MARKER'}),
                (applications[0], 'screened', {'score': 'PRIVATE SECRET MARKER', 'note': 'PRIVATE NOTE MARKER'}),
            ]
            ids = [db.execute('INSERT INTO audit_events(application_id,actor_id,action,details) VALUES (?,?,?,?)',
                              (app_id, actor, action, json.dumps(details))).lastrowid
                   for app_id, action, details in entries]
        yield client, app.state.database_path, applications, ids


def login(client, role):
    assert client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'}).status_code == 200


def test_role_checks_filters_and_no_free_text(example):
    client, _, applications, ids = example
    endpoint = '/api/audit/timeline'
    assert client.get(endpoint).status_code == 401
    for role in ('candidate', 'employee'):
        login(client, role)
        assert client.get(endpoint).status_code == 403
    login(client, 'hr')
    response = client.get(endpoint, params={'job_id': 'job-backend-python', 'action': 'screened'})
    assert response.status_code == 200
    body = response.json()
    assert [event['id'] for event in body['events']] == [ids[3], ids[0]]
    assert body['events'][0]['summary'] == {}
    assert body['events'][1]['summary'] == {'method': 'keyword_mentions_v1', 'score': 75,
        'required_mentioned': 3, 'required_total': 4, 'evidence_status': 'limited'}
    assert 'PRIVATE' not in response.text
    assert client.get(endpoint, params={'application_id': applications[1]}).json()['events'][0]['summary'] == {'decision': 'reject'}
    assert client.get(endpoint, params={'job_id': "job-backend-python' OR 1=1 --"}).json()['events'] == []


def test_pagination_is_stable_when_new_events_arrive(example):
    client, path, applications, ids = example
    login(client, 'hr')
    endpoint = '/api/audit/timeline'
    first = client.get(endpoint, params={'limit': 2}).json()
    assert [item['id'] for item in first['events']] == ids[-1:-3:-1]
    assert first['next_before'] == ids[2]
    with connect(path) as db:
        actor = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
        db.execute("INSERT INTO audit_events(application_id,actor_id,action,details) VALUES (?,?,'screened','{}')",
                   (applications[0], actor))
    second = client.get(endpoint, params={'limit': 2, 'before': first['next_before']}).json()
    assert [item['id'] for item in second['events']] == [ids[1], ids[0]]
    assert second['next_before'] is None
    assert client.get(endpoint, params={'limit': 101}).status_code == 422


def test_session_checked_before_response(example, monkeypatch):
    from app import audit_timeline

    client, _, _, _ = example
    login(client, 'hr')
    original = audit_timeline._require_current_role
    calls = 0

    def check(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(status_code=401, detail='Session expired')
        return original(*args)

    monkeypatch.setattr(audit_timeline, '_require_current_role', check)
    assert client.get('/api/audit/timeline').status_code == 401

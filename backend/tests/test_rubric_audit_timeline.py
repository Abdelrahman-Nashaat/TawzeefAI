"""Rubric audit browsing is bounded and excludes assessment content."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.fixture
def example(tmp_path):
    app = create_app(tmp_path / 'rubric-audit.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        with connect(app.state.database_path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            applications = [db.execute('''INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?,?,'cv.docx','PRIVATE RESUME MARKER')''', (candidate, job)).lastrowid
                for job in ('job-backend-python', 'job-frontend-react')]
            rows = [
                ('criterion_created', 'job-backend-python', None, 7),
                ('evidence_created', None, applications[0], 8),
                ('assessment_created', None, applications[0], 9),
                ('assessment_published', None, applications[1], 10),
            ]
            ids = [db.execute('''INSERT INTO rubric_audit_events(actor_id,action,job_id,application_id,reference_id)
                VALUES (?,?,?,?,?)''', (hr, *row)).lastrowid for row in rows]
        yield client, app.state.database_path, applications, ids


def login(client, role):
    assert client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'}).status_code == 200


def test_hr_only_filters_and_no_resume_content(example):
    client, _, applications, ids = example
    endpoint = '/api/audit/rubric'
    assert client.get(endpoint).status_code == 401
    for role in ('candidate', 'employee'):
        login(client, role)
        assert client.get(endpoint).status_code == 403
    login(client, 'hr')
    response = client.get(endpoint, params={'job_id': 'job-backend-python'})
    assert response.status_code == 200
    assert [item['id'] for item in response.json()['events']] == [ids[2], ids[1], ids[0]]
    assert 'PRIVATE' not in response.text
    assert [item['id'] for item in client.get(endpoint, params={'application_id': applications[0]}).json()['events']] == [ids[2], ids[1]]
    assert client.get(endpoint, params={'job_id': "job-backend-python' OR 1=1 --"}).json()['events'] == []
    assert client.get(endpoint, params={'limit': 101}).status_code == 422


def test_pagination_stable_when_new_event_arrives(example):
    client, path, applications, ids = example
    login(client, 'hr')
    first = client.get('/api/audit/rubric', params={'limit': 2}).json()
    assert [item['id'] for item in first['events']] == [ids[3], ids[2]]
    with connect(path) as db:
        hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
        db.execute("INSERT INTO rubric_audit_events(actor_id,action,application_id) VALUES (?,'assessment_withdrawn',?)", (hr, applications[0]))
    second = client.get('/api/audit/rubric', params={'limit': 2, 'before': first['next_before']}).json()
    assert [item['id'] for item in second['events']] == [ids[1], ids[0]]
    assert second['next_before'] is None


def test_session_checked_before_return(example, monkeypatch):
    from app import rubric_audit_timeline

    client, _, _, _ = example
    login(client, 'hr')
    original = rubric_audit_timeline._require_current_role
    calls = 0

    def check(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(status_code=401, detail='Session expired')
        return original(*args)

    monkeypatch.setattr(rubric_audit_timeline, '_require_current_role', check)
    assert client.get('/api/audit/rubric').status_code == 401

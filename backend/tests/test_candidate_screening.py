import json

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.db import connect
from app.main import create_app


@pytest.fixture
def preview(tmp_path):
    app = create_app(tmp_path / 'candidate-preview.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        with connect(app.state.database_path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            other = db.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES ('other','Other','candidate',?)", (hash_password('other-password'),)).lastrowid
            db.execute("UPDATE jobs SET required_skills=?,preferred_skills=? WHERE id='job-backend-python'", (json.dumps(['Python', 'SQL']), json.dumps(['Docker'])))
            app_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status,score_json) VALUES (?,'job-backend-python','synthetic.pdf','Built Python services.','advanced',?)", (candidate, json.dumps({'private_hr_note': 'confidential marker'}))).lastrowid
            other_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','other.pdf','Used SQL.','submitted')", (other,)).lastrowid
        yield client, app.state.database_path, app_id, other_id


def login(client, username, password):
    assert client.post('/api/auth/login', json={'username': username, 'password': password}).status_code == 200


def test_owner_gets_live_preview_without_hr_data(preview):
    client, path, app_id, _ = preview
    login(client, 'candidate', 'candidate-demo-only')
    endpoint = f'/api/applications/{app_id}/candidate-screening'
    before = client.get(endpoint)
    assert before.status_code == 200
    assert before.json()['mode'] == 'current_preview'
    assert before.json()['score'] == 50
    assert [item['mentioned'] for item in before.json()['required']] == [True, False]
    assert 'confidential marker' not in before.text
    with connect(path) as db:
        db.execute("UPDATE applications SET resume_text='Built Python and SQL services.' WHERE id=?", (app_id,))
    assert client.get(endpoint).json()['score'] == 100
    with connect(path) as db:
        db.execute("UPDATE jobs SET required_skills=? WHERE id='job-backend-python'", (json.dumps(['Docker']),))
    assert client.get(endpoint).json()['score'] == 0
    with connect(path) as db:
        assert json.loads(db.execute('SELECT score_json FROM applications WHERE id=?', (app_id,)).fetchone()[0]) == {'private_hr_note': 'confidential marker'}
        assert db.execute('SELECT count(*) FROM audit_events WHERE application_id=?', (app_id,)).fetchone()[0] == 0


def test_only_candidate_owner_can_read(preview):
    client, _, app_id, other_id = preview
    assert client.get(f'/api/applications/{app_id}/candidate-screening').status_code == 401
    login(client, 'candidate', 'candidate-demo-only')
    assert client.get(f'/api/applications/{other_id}/candidate-screening').status_code == 404
    for role in ('employee', 'hr'):
        login(client, role, f'{role}-demo-only')
        assert client.get(f'/api/applications/{app_id}/candidate-screening').status_code == 403


def test_expired_session_before_response_is_rejected(preview, monkeypatch):
    from fastapi import HTTPException

    from app import candidate_screening

    client, _, app_id, _ = preview
    login(client, 'candidate', 'candidate-demo-only')
    original = candidate_screening._require_current_role
    calls = 0

    def authorize(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(status_code=401, detail='Session expired')
        return original(*args)

    monkeypatch.setattr(candidate_screening, '_require_current_role', authorize)
    assert client.get(f'/api/applications/{app_id}/candidate-screening').status_code == 401

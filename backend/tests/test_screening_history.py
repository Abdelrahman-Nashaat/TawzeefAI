import json
import sqlite3

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.fixture
def setup(tmp_path):
    app = create_app(tmp_path / 'history.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        with connect(app.state.database_path) as db:
            candidate_id = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application_id = db.execute('''INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?,'job-backend-python','cv.docx','Built Python services.')''', (candidate_id,)).lastrowid
        yield client, app.state.database_path, application_id


def login(client, role):
    assert client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'}).status_code == 200


def test_history_retains_both_direct_and_workflow_results_and_denies_other_roles(setup):
    client, path, app_id = setup
    endpoint = f'/api/applications/{app_id}/screening-history'
    assert client.get(endpoint).status_code == 401
    login(client, 'hr')
    response = client.post(f'/api/workflows/{app_id}/run')
    assert response.status_code == 200
    first = client.get(f'/api/applications/{app_id}').json()['score']
    with connect(path) as db:
        db.execute("UPDATE applications SET resume_text='Used SQL and Docker.' WHERE id=?", (app_id,))
    response = client.post(f'/api/applications/{app_id}/screen')
    assert response.status_code == 200
    second = response.json()
    history = client.get(endpoint)
    assert history.status_code == 200
    entries = history.json()['entries']
    assert len(entries) == 2
    assert entries[0]['result'] == second
    assert entries[1]['result'] == first
    assert all(item['status'] == 'available' for item in entries)
    assert client.get(endpoint, params={'limit': 1}).json()['entries'][0] == entries[0]
    assert client.get(endpoint, params={'before': entries[0]['audit_event_id']}).json()['entries'] == entries[1:]
    for role in ('candidate', 'employee'):
        login(client, role)
        assert client.get(endpoint).status_code == 403


def test_history_marks_legacy_and_corrupt_snapshots_without_revealing_tampering(setup):
    client, path, app_id = setup
    login(client, 'hr')
    assert client.post(f'/api/applications/{app_id}/screen').status_code == 200
    with connect(path) as db:
        actor = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
        db.execute("INSERT INTO audit_events(application_id,actor_id,action,details) VALUES (?,?,'screened','{}')", (app_id, actor))
        db.execute("UPDATE screening_history SET score_json=?", (json.dumps({'unexpected': 'tampered marker'}),))
    data = client.get(f'/api/applications/{app_id}/screening-history').json()
    assert [item['status'] for item in data['entries']] == ['legacy_unavailable', 'integrity_error']
    assert all(item['result'] is None for item in data['entries'])
    assert 'tampered marker' not in json.dumps(data)


def test_write_failure_rolls_back_screen_and_audit(setup):
    client, path, app_id = setup
    login(client, 'hr')
    with connect(path) as db:
        db.execute("CREATE TRIGGER fail_history BEFORE INSERT ON screening_history BEGIN SELECT RAISE(ABORT, 'failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        client.post(f'/api/applications/{app_id}/screen')
    with connect(path) as db:
        assert db.execute('SELECT score_json FROM applications WHERE id=?', (app_id,)).fetchone()[0] is None
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='screened'", (app_id,)).fetchone()[0] == 0


def test_revoked_session_before_history_response_is_rejected(setup, monkeypatch):
    from app import recruitment

    client, _, app_id = setup
    login(client, 'hr')
    original = recruitment._require_current_role
    calls = 0

    def check(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(status_code=401, detail='Session expired')
        return original(*args)

    monkeypatch.setattr(recruitment, '_require_current_role', check)
    assert client.get(f'/api/applications/{app_id}/screening-history').status_code == 401


def test_workflow_failure_rolls_back_screen_and_checkpoint(setup):
    client, path, app_id = setup
    login(client, 'hr')
    with connect(path) as db:
        db.execute("CREATE TRIGGER fail_history BEFORE INSERT ON screening_history BEGIN SELECT RAISE(ABORT, 'failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        client.post(f'/api/workflows/{app_id}/run')
    with connect(path) as db:
        assert db.execute('SELECT score_json FROM applications WHERE id=?', (app_id,)).fetchone()[0] is None
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='screened'", (app_id,)).fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM recruitment_workflows WHERE application_id=?', (app_id,)).fetchone()[0] == 0

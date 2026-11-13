"""Audit timeline is HR-only, bounded and does not reveal raw event details."""
import json

from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401

from app.main import create_app


def test_hr_audit_metadata_and_pagination(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        job = candidate.get('/api/jobs').json()[0]['id']
        response = candidate.post('/api/applications', params={'job_id': job, 'filename': 'cv.docx'}, content=resume_bytes())
        assert response.status_code == 201
        application_id = response.json()['id']
        path = f'/api/applications/{application_id}/audit'
        assert candidate.get(path).status_code == 403
        login(hr, 'hr')
        assert hr.get('/api/applications/999999/audit').status_code == 404
        assert hr.get(path, params={'limit': 0}).status_code == 422
        assert hr.get(path, params={'before': 9223372036854775808}).status_code == 422
        assert hr.post(f'/api/applications/{application_id}/screen').status_code == 200
        with db_for(mode) as db:
            actor = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()['id']
            db.execute("INSERT INTO audit_events(application_id,actor_id,action,details) VALUES (%s,%s,'decision',%s)",
                       (application_id, actor, json.dumps({'decision': 'advance', 'note': 'private-note-for-test'})))
        first = hr.get(path, params={'limit': 2}).json()
        assert [item['action'] for item in first['items']] == ['decision', 'screened']
        assert first['items'][0]['decision'] == 'advance'
        assert 'private-note-for-test' not in repr(first)
        assert all(set(item) == {'id', 'actor_id', 'action', 'decision', 'created_at'} for item in first['items'])
        assert first['next_before'] == first['items'][-1]['id']
        second = hr.get(path, params={'limit': 2, 'before': first['next_before']}).json()
        assert second['items'] and second['next_before'] is None
        assert not {item['id'] for item in first['items']} & {item['id'] for item in second['items']}

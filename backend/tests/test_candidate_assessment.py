import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.db import connect
from app.main import create_app


def login(client, username, password=None):
    assert client.post('/api/auth/login', json={
        'username': username, 'password': password or f'{username}-demo-only',
    }).status_code == 200


@pytest.fixture
def scenario(tmp_path):
    app = create_app(tmp_path / 'publication.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        with connect(app.state.database_path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            other = db.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES ('other','Other','candidate',?)", (hash_password('other-password'),)).lastrowid
            app_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status,score_json) VALUES (?,'job-backend-python','cv.pdf','Python SQL','submitted',?)", (candidate, json.dumps({'private_hr_note': 'confidential marker'}))).lastrowid
            other_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','other.pdf','Other','submitted')", (other,)).lastrowid
        login(client, 'hr')
        criteria = []
        for category, label in [('mandatory', 'Python'), ('experience', 'Professional experience')]:
            response = client.post('/api/jobs/job-backend-python/rubric/criteria', json={'category': category, 'label': label})
            assert response.status_code == 201
            criteria.append(response.json()['criterion_ref'])
        evidence = client.post(f'/api/applications/{app_id}/rubric/evidence', json={'start': 0, 'end': 6}).json()['evidence_ref']
        result = client.post(f'/api/applications/{app_id}/rubric/assessment', json={'judgments': [
            {'criterion_ref': criteria[0], 'status': 'met', 'evidence_refs': [evidence]},
            {'criterion_ref': criteria[1], 'status': 'unknown'},
        ]})
        assert result.status_code == 201
        yield client, app.state.database_path, app_id, other_id


def release(client, app_id):
    path = f'/api/applications/{app_id}/rubric/candidate-publication'
    preview = client.get(path)
    assert preview.status_code == 200
    preview = preview.json()
    assert preview['status'] == 'current'
    payload = {key: preview[key] for key in ('assessment_id', 'snapshot_digest')}
    assert client.put(path, json=payload).status_code == 200
    return path, preview, payload


def test_explicit_release_and_withdrawal_are_private_and_owner_only(scenario):
    client, path, app_id, other_id = scenario
    candidate_path = f'/api/applications/{app_id}/candidate-assessment'
    assert client.get(candidate_path).status_code == 403
    publication_path, preview, _ = release(client, app_id)
    assert preview['preview']['bounds']['minimum'] < preview['preview']['bounds']['maximum']
    components = {item['category']: item for item in preview['preview']['components']}
    assert components['mandatory']['weight'] == 40
    assert components['mandatory']['minimum_points'] == components['mandatory']['maximum_points'] == 40.0
    assert components['experience']['minimum_points'] == 0.0
    assert components['experience']['maximum_points'] == 20.0
    assert components['education']['unconfigured'] is True
    assert all('claims' not in component for component in components.values())
    assert preview['preview']['criteria'][0]['evidence'] == ['Python']
    login(client, 'candidate')
    response = client.get(candidate_path)
    assert response.status_code == 200
    assert response.json() == preview['preview']
    assert 'confidential marker' not in response.text
    assert client.get(f'/api/applications/{other_id}/candidate-assessment').status_code == 404
    assert client.get(publication_path).status_code == 403
    assert client.put(publication_path, json={'assessment_id': preview['assessment_id'], 'snapshot_digest': preview['snapshot_digest']}).status_code == 403
    assert client.delete(publication_path).status_code == 403
    login(client, 'hr')
    assert client.delete(publication_path).status_code == 200
    login(client, 'candidate')
    assert client.get(candidate_path).json() == {'status': 'unavailable'}
    with connect(path) as db:
        assert [row[0] for row in db.execute("SELECT action FROM rubric_audit_events WHERE action LIKE 'assessment_%' ORDER BY id")] == ['assessment_created', 'assessment_published', 'assessment_withdrawn']


@pytest.mark.parametrize('mutation', ['resume', 'criterion', 'evidence', 'new_assessment'])
def test_changes_hide_release_and_reject_old_preview(scenario, mutation):
    client, path, app_id, _ = scenario
    publication_path, _, payload = release(client, app_id)
    if mutation == 'new_assessment':
        assert client.post(f'/api/applications/{app_id}/rubric/assessment', json={'judgments': []}).status_code == 201
    else:
        with connect(path) as db:
            if mutation == 'resume':
                db.execute("UPDATE applications SET resume_text='Changed resume' WHERE id=?", (app_id,))
            elif mutation == 'criterion':
                db.execute("UPDATE rubric_criteria SET label='Changed' WHERE job_id='job-backend-python' AND category='mandatory'")
            else:
                db.execute("UPDATE rubric_evidence SET excerpt='changed' WHERE application_id=?", (app_id,))
    assert client.put(publication_path, json=payload).status_code == 409
    login(client, 'candidate')
    assert client.get(f'/api/applications/{app_id}/candidate-assessment').json() == {'status': 'unavailable'}


def test_session_rechecked_and_read_does_not_write(scenario, monkeypatch):
    from app import candidate_screening

    client, path, app_id, _ = scenario
    release(client, app_id)
    login(client, 'candidate')
    endpoint = f'/api/applications/{app_id}/candidate-assessment'
    with connect(path) as db:
        before = db.execute('SELECT count(*) FROM rubric_audit_events').fetchone()[0]
    assert client.get(endpoint).status_code == 200
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM rubric_audit_events').fetchone()[0] == before
    original = candidate_screening._require_current_role
    calls = 0

    def authorize(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(status_code=401, detail='Session expired')
        return original(*args)

    monkeypatch.setattr(candidate_screening, '_require_current_role', authorize)
    assert client.get(endpoint).status_code == 401

@pytest.mark.parametrize('mutation', ['resume', 'criterion', 'evidence'])
def test_changed_then_reverted_input_requires_a_new_preview(scenario, mutation):
    client, path, app_id, _ = scenario
    publication_path, _, payload = release(client, app_id)
    with connect(path) as db:
        if mutation == 'resume':
            row = db.execute('SELECT resume_text FROM applications WHERE id=?', (app_id,)).fetchone()
            db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('changed', app_id))
            db.execute('UPDATE applications SET resume_text=? WHERE id=?', (row[0], app_id))
        elif mutation == 'criterion':
            row = db.execute("SELECT id,label FROM rubric_criteria WHERE job_id='job-backend-python' LIMIT 1").fetchone()
            db.execute('UPDATE rubric_criteria SET label=? WHERE id=?', ('changed', row['id']))
            db.execute('UPDATE rubric_criteria SET label=? WHERE id=?', (row['label'], row['id']))
        else:
            row = db.execute('SELECT id,excerpt FROM rubric_evidence WHERE application_id=? LIMIT 1', (app_id,)).fetchone()
            db.execute('UPDATE rubric_evidence SET excerpt=? WHERE id=?', ('changed', row['id']))
            db.execute('UPDATE rubric_evidence SET excerpt=? WHERE id=?', (row['excerpt'], row['id']))
    assert client.put(publication_path, json=payload).status_code == 409
    refreshed = client.get(publication_path).json()
    if mutation == 'resume':
        assert refreshed['status'] == 'outdated'
        assert refreshed['snapshot_digest'] is None
    else:
        assert refreshed['status'] == 'current'
        assert refreshed['snapshot_digest'] != payload['snapshot_digest']
    login(client, 'candidate')
    assert client.get(f'/api/applications/{app_id}/candidate-assessment').json() == {'status': 'unavailable'}


def test_changed_rendering_keeps_stale_publication_withdrawable(scenario, monkeypatch):
    from app import candidate_screening

    client, _, app_id, _ = scenario
    publication_path, _, _ = release(client, app_id)
    original = candidate_screening._candidate_assessment

    def changed_preview(snapshot):
        preview = original(snapshot)
        preview['notice'] += ' Changed rendering.'
        return preview

    monkeypatch.setattr(candidate_screening, '_candidate_assessment', changed_preview)
    result = client.get(publication_path).json()
    assert result['published'] is False
    assert result['has_publication'] is True
    assert client.delete(publication_path).status_code == 200


def test_reviewed_status_transition_preserves_current_candidate_assessment(scenario):
    client, _, app_id, _ = scenario
    _, prior, _ = release(client, app_id)
    review = client.post(f'/api/applications/{app_id}/hr-review-preview').json()
    response = client.post(f'/api/applications/{app_id}/decision', json={
        'decision': 'advance', 'review_reference': review['reference']})
    assert response.status_code == 200
    login(client, 'candidate')
    assert client.get(f'/api/applications/{app_id}/candidate-assessment').json() == prior['preview']

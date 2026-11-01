"""Reviewed evidence is saved atomically without changing assessment judgments."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import rubric_evidence_bundle
from app.db import connect
from app.main import create_app


@pytest.fixture
def case(tmp_path):
    app = create_app(tmp_path / 'bundle.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(app.state.database_path) as db:
            user_id = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application_id = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text,status)
                VALUES (?,'job-backend-python','sample.txt','Python SQL','submitted')""", (user_id,)).lastrowid
        criteria = []
        for label in ('Python', 'SQL'):
            response = client.post('/api/jobs/job-backend-python/rubric/criteria',
                                   json={'category': 'mandatory', 'label': label})
            assert response.status_code == 201
            criteria.append(response.json()['criterion_ref'])
        yield client, app.state.database_path, application_id, criteria


def context(client, application_id):
    response = client.get(f'/api/applications/{application_id}/rubric/evidence-bundle/context')
    assert response.status_code == 200
    return response.json()['snapshot']


def payload(snapshot, criteria):
    return {'snapshot': snapshot, 'items': [
        {'criterion_ref': criteria[0], 'start': 0, 'end': 6, 'excerpt': 'Python', 'reviewed': True},
        {'criterion_ref': criteria[1], 'start': 7, 'end': 10, 'excerpt': 'SQL', 'reviewed': True},
    ]}


def test_bundle_saves_and_audits_without_judgments(case):
    client, path, app_id, criteria = case
    endpoint = f'/api/applications/{app_id}/rubric/evidence-bundle'
    response = client.post(endpoint, json=payload(context(client, app_id), criteria))
    assert response.status_code == 200
    assert [item['evidence']['excerpt'] for item in response.json()['items']] == ['Python', 'SQL']
    assert all(item['created'] for item in response.json()['items'])
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM rubric_evidence').fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM rubric_audit_events WHERE action='evidence_created'").fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM rubric_assessments').fetchone()[0] == 0
    result = client.post(endpoint, json=payload(context(client, app_id), criteria))
    assert result.status_code == 200
    assert not any(item['created'] for item in result.json()['items'])
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM rubric_evidence').fetchone()[0] == 2


@pytest.mark.parametrize('mutation', ['source_reverted', 'criterion_reverted', 'owner_reverted'])
def test_changed_then_restored_inputs_invalidate_review(case, mutation):
    client, path, app_id, criteria = case
    snapshot = context(client, app_id)
    with connect(path) as db:
        if mutation == 'source_reverted':
            db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('different', app_id))
            db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('Python SQL', app_id))
        elif mutation == 'criterion_reverted':
            db.execute("UPDATE rubric_criteria SET label='Different' WHERE id=?", (int(criteria[0][2:]),))
            db.execute("UPDATE rubric_criteria SET label='Python' WHERE id=?", (int(criteria[0][2:]),))
        else:
            candidate = db.execute('SELECT candidate_id FROM applications WHERE id=?', (app_id,)).fetchone()[0]
            other = db.execute("SELECT id FROM users WHERE username='employee'").fetchone()[0]
            db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (other, app_id))
            db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (candidate, app_id))
    response = client.post(f'/api/applications/{app_id}/rubric/evidence-bundle', json=payload(snapshot, criteria))
    assert response.status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM rubric_evidence').fetchone()[0] == 0


def test_invalid_passage_is_atomic_and_candidate_cannot_access(case):
    client, path, app_id, criteria = case
    request = payload(context(client, app_id), criteria)
    request['items'][1]['excerpt'] = 'not present'
    endpoint = f'/api/applications/{app_id}/rubric/evidence-bundle'
    assert client.post(endpoint, json=request).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM rubric_evidence').fetchone()[0] == 0
    assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
    assert client.get(endpoint + '/context').status_code == 403
    assert client.post(endpoint, json=request).status_code == 403


def test_role_expiry_rolls_back_all_writes(case, monkeypatch):
    client, path, app_id, criteria = case
    request = payload(context(client, app_id), criteria)
    original = rubric_evidence_bundle._require_current_role
    calls = 0

    def check(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(status_code=401, detail='Session expired')
        return original(*args)

    monkeypatch.setattr(rubric_evidence_bundle, '_require_current_role', check)
    response = client.post(f'/api/applications/{app_id}/rubric/evidence-bundle', json=request)
    assert response.status_code == 401
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM rubric_evidence').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM rubric_audit_events WHERE action='evidence_created'").fetchone()[0] == 0


def test_shared_passage_is_reused_across_criteria(case):
    client, path, app_id, criteria = case
    request = payload(context(client, app_id), criteria)
    request['items'][1] = {**request['items'][0], 'criterion_ref': criteria[1]}
    response = client.post(f'/api/applications/{app_id}/rubric/evidence-bundle', json=request)
    assert response.status_code == 200
    first, second = response.json()['items']
    assert first['created'] and not second['created']
    assert first['evidence']['evidence_ref'] == second['evidence']['evidence_ref']
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM rubric_evidence').fetchone()[0] == 1


def test_each_passage_requires_explicit_review_and_duplicates_are_rejected(case):
    client, path, app_id, criteria = case
    request = payload(context(client, app_id), criteria)
    request['items'][1]['reviewed'] = False
    endpoint = f'/api/applications/{app_id}/rubric/evidence-bundle'
    assert client.post(endpoint, json=request).status_code == 422
    request['items'][1] = dict(request['items'][0])
    assert client.post(endpoint, json=request).status_code == 422
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM rubric_evidence').fetchone()[0] == 0

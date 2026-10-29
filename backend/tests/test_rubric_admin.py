import json
from io import BytesIO

import pytest
from docx import Document
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.fixture
def state(tmp_path):
    path = tmp_path / 'rubric.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        yield client, path


def login(client, role):
    response = client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'})
    assert response.status_code == 200
    return response.json()


def application(client, text='Name: Sample\nBuilt Python and SQL services.'):
    document = Document()
    for line in text.splitlines():
        document.add_paragraph(line)
    data = BytesIO()
    document.save(data)
    response = client.post('/api/applications', params={
        'job_id': 'job-backend-python', 'filename': 'resume.docx',
    }, content=data.getvalue())
    assert response.status_code == 201
    return response.json()['id']


def test_assessment_uses_server_actor_evidence_and_preserves_application_status(state):
    client, path = state
    login(client, 'candidate')
    application_id = application(client)
    login(client, 'hr')
    hr = client.get('/api/auth/me').json()
    criterion = client.post('/api/jobs/job-backend-python/rubric/criteria', json={
        'category': 'mandatory', 'label': 'Python',
    }).json()['criterion_ref']
    second = client.post('/api/jobs/job-backend-python/rubric/criteria', json={
        'category': 'mandatory', 'label': 'SQL',
    }).json()['criterion_ref']
    source = client.get(f'/api/applications/{application_id}/rubric/source').json()
    assert 'Name: Sample' not in source['text'] and source['requires_review']
    offset = source['text'].index('Python')
    evidence = client.post(f'/api/applications/{application_id}/rubric/evidence', json={
        'start': offset, 'end': offset + len('Python'),
    })
    assert evidence.status_code == 201 and evidence.json()['excerpt'] == 'Python'
    evidence_ref = evidence.json()['evidence_ref']
    response = client.post(f'/api/applications/{application_id}/rubric/assessment', json={
        'judgments': [{'criterion_ref': criterion, 'status': 'met', 'evidence_refs': [evidence_ref]}],
    })
    assert response.status_code == 201
    result = response.json()
    assert result['actor_id'] == hr['id']
    assert result['result']['minimum_score'] == 20 and result['result']['maximum_score'] == 100
    assert result['result']['mandatory_flags']['unknown'] == [second]
    assert client.get(f'/api/applications/{application_id}').json()['status'] == 'submitted'
    saved = client.get(f'/api/applications/{application_id}/rubric/assessment').json()
    assert saved['result'] == result['result'] and saved['actor_id'] == hr['id'] and not saved['outdated']
    assert client.post('/api/jobs/job-backend-python/rubric/criteria', json={
        'category': 'projects', 'label': 'Service project',
    }).status_code == 201
    assert client.get(f'/api/applications/{application_id}/rubric/assessment').json()['outdated'] is True
    assert all(event['actor_id'] == hr['id'] for event in client.get('/api/rubric/audit').json())
    with connect(path) as db:
        audit = [dict(row) for row in db.execute('SELECT * FROM rubric_audit_events')]
    assert len(audit) == 5 and all(event['actor_id'] == hr['id'] for event in audit)
    assert 'Python' not in str(audit) and 'Sample' not in str(audit)


def test_rejects_cross_job_and_cross_application_refs_and_unknown_evidence(state):
    client, _ = state
    login(client, 'candidate')
    first, second = application(client), application(client, 'Built SQL reporting.')
    login(client, 'hr')
    c1 = client.post('/api/jobs/job-backend-python/rubric/criteria', json={
        'category': 'mandatory', 'label': 'Python',
    }).json()['criterion_ref']
    c2 = client.post('/api/jobs/job-data-engineer/rubric/criteria', json={
        'category': 'experience', 'label': 'ETL',
    })
    assert c2.status_code == 201
    evidence = client.post(f'/api/applications/{second}/rubric/evidence', json={'start': 0, 'end': 5})
    assert evidence.status_code == 201
    ref = evidence.json()['evidence_ref']
    target = f'/api/applications/{first}/rubric/assessment'
    assert client.post(target, json={'judgments': [{'criterion_ref': c1, 'status': 'met', 'evidence_refs': [ref]}]}).status_code == 422
    assert client.post(target, json={'judgments': [{'criterion_ref': c2.json()['criterion_ref'], 'status': 'unknown'}]}).status_code == 422
    assert client.post(target, json={'judgments': [{'criterion_ref': c1, 'status': 'unknown', 'evidence_refs': [ref]}]}).status_code == 422
    assert client.post(target, json={'judgments': [{'criterion_ref': c1, 'status': 'met', 'evidence_refs': ['e-9999']}]}).status_code == 422
    assert client.post(target, json={'judgments': [{'criterion_ref': c1, 'status': 'missing'}]}).status_code == 422
    assert client.get(target).status_code == 404


def test_role_access_and_input_boundaries(state):
    client, _ = state
    criteria = '/api/jobs/job-backend-python/rubric/criteria'
    assert client.get(criteria).status_code == 401
    login(client, 'candidate')
    application_id = application(client)
    target = f'/api/applications/{application_id}/rubric'
    assert client.get(criteria).status_code == 403
    assert client.post(criteria, json={'category': 'mandatory', 'label': 'Python'}).status_code == 403
    assert client.get(target + '/source').status_code == 403
    assert client.get('/api/rubric/audit').status_code == 403
    assert client.post(target + '/assessment', json={'judgments': []}).status_code == 403
    login(client, 'employee')
    assert client.get(target + '/evidence').status_code == 403
    login(client, 'hr')
    assert client.post(criteria, json={'category': 'mandatory', 'label': 'Name: Alice'}).status_code == 422
    assert client.post(criteria, json={'category': 'mandatory', 'label': 'person@example.com'}).status_code == 422
    assert client.post(target + '/evidence', json={'start': 0, 'end': 9999}).status_code == 422
    assert client.post(target + '/assessment', json={'judgments': [], 'actor_id': 3}).status_code == 422


def test_assessment_persists_across_app_restart(tmp_path):
    path = tmp_path / 'restart.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        login(client, 'candidate')
        application_id = application(client)
        login(client, 'hr')
        assert client.post('/api/jobs/job-backend-python/rubric/criteria', json={
            'category': 'mandatory', 'label': 'Python',
        }).status_code == 201
        response = client.post(f'/api/applications/{application_id}/rubric/assessment', json={'judgments': []})
        assert response.status_code == 201 and response.json()['result']['coverage_percent'] == 0
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        login(client, 'hr')
        result = client.get(f'/api/applications/{application_id}/rubric/assessment')
        assert result.status_code == 200 and result.json()['result']['coverage_percent'] == 0


def test_missing_requires_explicit_attestation_and_persists_it(state):
    client, path = state
    login(client, 'candidate')
    application_id = application(client, 'Built Python services.')
    login(client, 'hr')
    criterion = client.post('/api/jobs/job-backend-python/rubric/criteria', json={
        'category': 'mandatory', 'label': 'SQL',
    }).json()['criterion_ref']
    source = client.get(f'/api/applications/{application_id}/rubric/source').json()['text']
    start = source.index('Python')
    evidence_ref = client.post(f'/api/applications/{application_id}/rubric/evidence',
                               json={'start': start, 'end': start + 6}).json()['evidence_ref']
    target = f'/api/applications/{application_id}/rubric/assessment'
    claim = {'criterion_ref': criterion, 'status': 'missing', 'evidence_refs': [evidence_ref]}
    assert client.post(target, json={'judgments': [claim]}).status_code == 422
    assert client.post(target, json={'judgments': [{**claim, 'absence_review_attested': 'true'}]}).status_code == 422
    unknown = client.post(target, json={'judgments': []}).json()['result']
    assert unknown['coverage_percent'] == 0 and unknown['maximum_score'] == 100
    response = client.post(target, json={'judgments': [{**claim, 'absence_review_attested': True}]})
    assert response.status_code == 201
    saved = client.get(target).json()
    assert saved['result']['components'][0]['claims'][0]['absence_review_attested'] is True
    assert saved['result']['mandatory_flags']['missing'] == [criterion]
    # Simulate an assessment saved before explicit attestation was required.
    with connect(path) as db:
        row = db.execute('SELECT id, result_json FROM rubric_assessments ORDER BY id DESC LIMIT 1').fetchone()
        old = json.loads(row['result_json'])
        del old['components'][0]['claims'][0]['absence_review_attested']
        db.execute('UPDATE rubric_assessments SET result_json = ? WHERE id = ?', (json.dumps(old), row['id']))
    legacy = client.get(target).json()
    assert legacy['outdated'] and legacy['requires_absence_review']
    assert legacy['result']['mandatory_flags']['unknown'] == [criterion]
    assert legacy['result']['coverage_percent'] == 0 and legacy['result']['maximum_score'] == 100

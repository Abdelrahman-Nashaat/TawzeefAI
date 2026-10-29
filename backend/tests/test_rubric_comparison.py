import json

from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app
from app.rubric_comparison import router


def login(client, role):
    assert client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'}).status_code == 200


def test_comparison_is_authorized_uncertainty_aware_and_ignores_outdated(tmp_path):
    app = create_app(tmp_path / 'comparison.sqlite3', seed_demo=True)
    # Module is deliberately mounted here until integrated in main by the caller.
    app.include_router(router)
    with TestClient(app) as client:
        path = '/api/jobs/job-backend-python/rubric/comparison'
        assert client.get(path).status_code == 401
        login(client, 'candidate')
        assert client.get(path).status_code == 403
        with connect(app.state.database_path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()['id']
            ids = [db.execute('INSERT INTO applications (candidate_id,job_id,filename,resume_text) VALUES (?,?,?,?)',
                              (candidate, 'job-backend-python', 'synthetic.docx', 'Python SQL')).lastrowid
                   for _ in range(4)]
        login(client, 'employee')
        assert client.get(path).status_code == 403
        login(client, 'hr')
        assert client.get('/api/jobs/no-such-job/rubric/comparison').status_code == 404
        criteria = []
        for category in ('mandatory', 'preferred', 'experience', 'projects', 'education'):
            response = client.post('/api/jobs/job-backend-python/rubric/criteria', json={
                'category': category, 'label': f'{category} criterion',
            })
            assert response.status_code == 201
            criteria.append(response.json()['criterion_ref'])
        for app_id, status in zip(ids[:3], ('met', 'missing', 'unknown'), strict=True):
            evidence = client.post(f'/api/applications/{app_id}/rubric/evidence', json={'start': 0, 'end': 6})
            assert evidence.status_code == 201
            ref = evidence.json()['evidence_ref']
            judgments = []
            for criterion in criteria:
                judgment = {'criterion_ref': criterion, 'status': status}
                if status != 'unknown':
                    judgment['evidence_refs'] = [ref]
                if status == 'missing':
                    judgment['absence_review_attested'] = True
                judgments.append(judgment)
            result = client.post(f'/api/applications/{app_id}/rubric/assessment', json={'judgments': judgments})
            assert result.status_code == 201
        response = client.get(path)
        assert response.status_code == 200
        result = response.json()
        items = {item['application_id']: item for item in result['applications']}
        assert items[ids[0]]['bounds'] == {'minimum': 100.0, 'maximum': 100.0, 'coverage_percent': 100.0}
        assert items[ids[1]]['bounds']['maximum'] == 0
        assert items[ids[2]]['bounds']['minimum'] == 0 and items[ids[2]]['bounds']['maximum'] == 100
        assert items[ids[3]]['status'] == 'not_assessed'
        assert items[ids[0]]['questions'] == []
        assert len(items[ids[1]]['questions']) == len(items[ids[2]]['questions']) == 5
        relations = {(r['left_application_id'], r['right_application_id']): r['relation'] for r in result['comparisons']}
        assert relations == {(ids[0], ids[1]): 'left_higher', (ids[0], ids[2]): 'inconclusive',
                             (ids[1], ids[2]): 'inconclusive'}
        assert 'Python SQL' not in str(result)
        # Legacy absence claims without explicit attestation are ineligible.
        with connect(app.state.database_path) as db:
            row = db.execute('SELECT id, assessment_json FROM rubric_assessments WHERE application_id = ?', (ids[1],)).fetchone()
            legacy = json.loads(row['assessment_json'])
            del legacy['mandatory'][0]['absence_review_attested']
            db.execute('UPDATE rubric_assessments SET assessment_json = ? WHERE id = ?', (json.dumps(legacy), row['id']))
        result = client.get(path).json()
        stale = next(item for item in result['applications'] if item['application_id'] == ids[1])
        assert stale['outdated'] and stale['bounds'] is None and not stale['questions']
        assert all(ids[1] not in (pair['left_application_id'], pair['right_application_id']) for pair in result['comparisons'])
        # Changing criteria invalidates all previous comparisons.
        client.post('/api/jobs/job-backend-python/rubric/criteria', json={'category': 'mandatory', 'label': 'New criterion'})
        result = client.get(path).json()
        assert result['comparisons'] == []
        assert all(item['outdated'] for item in result['applications'] if item['assessment_id'] is not None)

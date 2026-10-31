import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver

from app.db import connect
from app.graph_orchestrator import _config, _view, build_review_graph
from app.main import create_app


def login(client, role):
    assert client.post('/api/auth/login', json={
        'username': role, 'password': f'{role}-demo-only',
    }).status_code == 200


def test_matching_graph_exposes_reviewed_evidence_and_fails_closed_on_change(tmp_path):
    path = tmp_path / 'matching.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        source = 'Skills\nPython services\nProjects\nBuilt a reliable worker'
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute('''INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?, 'job-backend-python', 'synthetic.docx', ?)''', (candidate, source)).lastrowid

        criteria_endpoint = '/api/jobs/job-backend-python/rubric/criteria'
        required = client.post(criteria_endpoint, json={'category': 'mandatory', 'label': 'Python'}).json()
        projects = client.post(criteria_endpoint, json={'category': 'projects', 'label': 'Reliable worker'}).json()
        evidence = client.post(f'/api/applications/{app_id}/rubric/evidence', json={
            'start': source.index('Python services'), 'end': source.index('Python services') + len('Python services'),
        }).json()
        assessment = client.post(f'/api/applications/{app_id}/rubric/assessment', json={
            'judgments': [{'criterion_ref': required['criterion_ref'], 'status': 'met',
                           'evidence_refs': [evidence['evidence_ref']]}],
        })
        assert assessment.status_code == 201, assessment.text
        with connect(path) as db:
            row = db.execute('SELECT id,result_json FROM rubric_assessments WHERE application_id=?', (app_id,)).fetchone()
            altered = json.loads(row['result_json'])
            altered['minimum_score'] = 99
            db.execute('UPDATE rubric_assessments SET result_json=? WHERE id=?', (json.dumps(altered), row['id']))
        assert client.get(f'/api/applications/{app_id}/rubric/assessment').json()['result']['minimum_score'] == 40
        response = client.post('/api/graph-reviews', json={'application_id': app_id})
        assert response.status_code == 201, response.text
        review = response.json()
        report = review['state']['matching_specialist']
        assert report['status'] == 'current'
        assert report['bounds']['minimum'] == 40
        python = next(item for item in report['criteria'] if item['criterion_ref'] == required['criterion_ref'])
        assert python['status'] == 'met'
        assert python['evidence'][0]['excerpt'] == 'Python services'
        project = next(item for item in report['criteria'] if item['criterion_ref'] == projects['criterion_ref'])
        assert project['status'] == 'unknown' and project['evidence'] == [] and project['question_ar']
        with SqliteSaver.from_conn_string(str(path)) as saver:
            snapshot = build_review_graph(path, saver).get_state(_config(review['id']))
            assert 'Python services' not in str(snapshot.values)
            assert 'Reliable worker' not in str(snapshot.values)
        with connect(path) as db:
            original_job = db.execute("SELECT required_skills FROM jobs WHERE id='job-backend-python'").fetchone()[0]
            db.execute("UPDATE jobs SET required_skills='[\"Rust\"]' WHERE id='job-backend-python'")
        stale_job = client.get(f"/api/graph-reviews/{review['id']}").json()['state']
        assert stale_job['matching_specialist']['status'] == 'stale' and 'matching' not in stale_job
        with connect(path) as db:
            db.execute("UPDATE jobs SET required_skills=? WHERE id='job-backend-python'", (original_job,))
            assert db.execute('SELECT count(*) FROM rubric_assessments').fetchone()[0] == 1
            db.execute('UPDATE rubric_evidence SET excerpt=? WHERE id=?', ('changed', int(evidence['evidence_ref'][2:])))
        updated = client.get(f"/api/graph-reviews/{review['id']}").json()
        assert updated['state']['matching_specialist']['status'] == 'stale'
        assert 'matching' not in updated['state']
        assert 'Python services' not in str(updated['state']['matching_specialist'])
        assert client.get(f'/api/applications/{app_id}/rubric/assessment').json()['outdated'] is True
        comparison = client.get('/api/jobs/job-backend-python/rubric/comparison').json()
        assert next(item for item in comparison['applications'] if item['application_id'] == app_id)['status'] == 'outdated'
        assert client.post(f'/api/applications/{app_id}/rubric/assessment', json={'judgments': [
            {'criterion_ref': required['criterion_ref'], 'status': 'met', 'evidence_refs': [evidence['evidence_ref']]},
        ]}).status_code == 409
        login(client, 'candidate')
        assert client.get(f"/api/graph-reviews/{review['id']}").status_code == 403


def test_assessment_fingerprint_detects_changed_labels_and_unversioned_history(tmp_path):
    path = tmp_path / 'matching-label.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute('''INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?, 'job-backend-python', 'synthetic.docx', 'Skills\nPython')''', (candidate,)).lastrowid
        criterion = client.post('/api/jobs/job-backend-python/rubric/criteria', json={
            'category': 'mandatory', 'label': 'Python',
        }).json()
        endpoint = f'/api/applications/{app_id}/rubric/assessment'
        assert client.post(endpoint, json={'judgments': []}).status_code == 201
        assert client.get(endpoint).json()['outdated'] is False
        with connect(path) as db:
            db.execute('UPDATE rubric_criteria SET label=? WHERE id=?', ('Rust', int(criterion['criterion_ref'][2:])))
        assert client.get(endpoint).json()['outdated'] is True
        comparison = client.get('/api/jobs/job-backend-python/rubric/comparison').json()
        assert next(item for item in comparison['applications'] if item['application_id'] == app_id)['status'] == 'outdated'
        review = client.post('/api/graph-reviews', json={'application_id': app_id}).json()
        assert review['state']['matching_specialist']['status'] == 'outdated'
        assert client.post(endpoint, json={'judgments': []}).status_code == 201
        assert client.get(endpoint).json()['outdated'] is False
        review = client.post('/api/graph-reviews', json={'application_id': app_id}).json()
        assert review['state']['matching_specialist']['status'] == 'current'
        with connect(path) as db:
            db.execute('UPDATE applications SET resume_text=? WHERE id=?', ('Skills\nPython\nProjects\nNew work', app_id))
        assert client.get(endpoint).json()['outdated'] is True
        review = client.post('/api/graph-reviews', json={'application_id': app_id}).json()
        assert review['state']['matching_specialist']['status'] == 'outdated'
        assert client.post(endpoint, json={'judgments': []}).status_code == 201
        assert client.get(endpoint).json()['outdated'] is False
        with connect(path) as db:
            db.execute('UPDATE rubric_assessments SET criteria_fingerprint=NULL WHERE application_id=?', (app_id,))
        assert client.get(endpoint).json()['outdated'] is True
        assert client.post('/api/graph-reviews', json={'application_id': app_id}).json()['state']['matching_specialist']['status'] == 'outdated'


def test_matching_graph_distinguishes_not_assessed_and_outdated(tmp_path):
    path = tmp_path / 'matching-missing.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute('''INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?, 'job-backend-python', 'synthetic.docx', 'Skills\nPython')''', (candidate,)).lastrowid
        review = client.post('/api/graph-reviews', json={'application_id': app_id}).json()
        assert review['state']['matching_specialist']['status'] == 'not_assessed'
        criterion = client.post('/api/jobs/job-backend-python/rubric/criteria', json={
            'category': 'mandatory', 'label': 'Python',
        }).json()
        assert client.post(f'/api/applications/{app_id}/rubric/assessment', json={'judgments': []}).status_code == 201
        client.post('/api/jobs/job-backend-python/rubric/criteria', json={'category': 'preferred', 'label': 'SQL'})
        current = client.post('/api/graph-reviews', json={'application_id': app_id}).json()
        assert current['state']['matching_specialist']['status'] == 'outdated'
        assert criterion['criterion_ref'] not in str(current['state']['matching_specialist'])


def test_legacy_matching_checkpoint_without_snapshot_is_not_exposed():
    legacy = SimpleNamespace(values={'application_id': 1, 'matching': {'score': 100}}, next=('human_review',))
    result = _view(legacy)['state']
    assert 'matching' not in result
    assert result['matching_specialist']['status'] == 'stale'

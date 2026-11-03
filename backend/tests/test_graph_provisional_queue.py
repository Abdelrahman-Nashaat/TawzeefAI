"""Full graph reviews expose current provisional facts, never checkpointed CV text."""

from fastapi.testclient import TestClient

from app.auto_provisional import run_one
from app.db import connect
from app.main import create_app
from app.provisional_queue import create_pending_snapshot


def login(client, role):
    assert client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'}).status_code == 200


def test_full_graph_reconstructs_live_provisional_facts_and_revokes_access(tmp_path):
    path = tmp_path / 'graph-provisional.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?,'job-backend-python','synthetic.docx','Skills: Python, SQL; PRIVATE MARKER')", (candidate,)).lastrowid
            create_pending_snapshot(db, app_id, 'job-backend-python', candidate)
        response = client.post('/api/graph-reviews', json={'application_id': app_id})
        assert response.status_code == 201, response.text
        review_id = response.json()['id']
        initial = response.json()['state']['provisional_screening']
        assert initial['recommendation'] == 'pending' and initial['score'] is None
        assert initial['threshold'] is None and initial['origin'] is None
        assert run_one(path)
        current = client.get(f'/api/graph-reviews/{review_id}').json()['state']['provisional_screening']
        assert current['origin'] == 'system_worker' and current['current']
        assert current['recommendation'] == 'mask_review_required'
        assert isinstance(current['score'], int)
        assert client.put('/api/jobs/job-backend-python/provisional-threshold', json={'threshold': 99, 'expected_version': 0}).status_code == 200
        changed_threshold = client.get(f'/api/graph-reviews/{review_id}').json()['state']['provisional_screening']
        assert changed_threshold['threshold'] == 99 and changed_threshold['threshold_version'] == 1
        assert changed_threshold['recommendation'] == 'mask_review_required'
        mask = client.get(f'/api/applications/{app_id}/resume/mask').json()
        assert client.put(f'/api/applications/{app_id}/resume/mask', json={'source_sha256': mask['source_sha256'], 'expected_source_revision': mask['source_revision'], 'expected_revision': mask['revision'], 'spans': []}).status_code == 200
        stale = client.get(f'/api/graph-reviews/{review_id}').json()['state']['provisional_screening']
        assert stale['recommendation'] == 'stale' and stale['score'] is None
        refreshed = client.post(f'/api/applications/{app_id}/provisional-screening', json={'expected_snapshot_id': stale['snapshot_id']})
        assert refreshed.status_code == 201
        reviewed = client.get(f'/api/graph-reviews/{review_id}').json()['state']['provisional_screening']
        assert reviewed['recommendation'] == 'needs_review' and reviewed['threshold'] == 99
        assert client.put('/api/jobs/job-backend-python/provisional-threshold', json={'threshold': 0, 'expected_version': 1}).status_code == 200
        reclassified = client.get(f'/api/graph-reviews/{review_id}').json()['state']['provisional_screening']
        assert reclassified['recommendation'] == 'priority_review' and reclassified['threshold_version'] == 2
        with connect(path) as db:
            db.execute("UPDATE jobs SET title='Changed' WHERE id='job-backend-python'")
            db.execute("UPDATE jobs SET title='Python Backend Developer' WHERE id='job-backend-python'")
        after_job = client.get(f'/api/graph-reviews/{review_id}').json()['state']['provisional_screening']
        assert after_job['recommendation'] == 'stale' and after_job['score'] is None
        with connect(path) as db:
            saved = repr([tuple(row) for row in db.execute('SELECT checkpoint FROM checkpoints').fetchall()])
            assert 'PRIVATE MARKER' not in saved
        assert client.post('/api/auth/logout').status_code == 204
        assert client.get(f'/api/graph-reviews/{review_id}').status_code == 401
        login(client, 'candidate')
        assert client.get(f'/api/graph-reviews/{review_id}').status_code == 403
        assert client.post(f'/api/graph-reviews/{review_id}/resume', json={'reviewed': True}).status_code == 403

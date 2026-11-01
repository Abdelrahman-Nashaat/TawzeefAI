"""Semantic specialist suggestions remain source-bound and HR-only."""

import os
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    from app import semantic_screening

    monkeypatch.setenv('TAWZEEF_ENABLE_SEMANTIC_SCREENING', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'local')

    class Model:
        def encode(self, texts, *, query):
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(semantic_screening, '_model', lambda _: Model())
    path = tmp_path / 'review.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        with connect(path) as db:
            owner = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text,status)
                VALUES (?,'job-backend-python','synthetic.txt','Skills\nUniquePythonEvidenceForReview','submitted')""",
                (owner,)).lastrowid
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        assert client.post('/api/jobs/job-backend-python/rubric/criteria', json={
            'category': 'mandatory', 'label': 'Python',
        }).status_code == 201
        yield client, path, app_id


def test_review_persists_only_references_and_reconstructs(scenario):
    client, path, application = scenario
    response = client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'semantic_review'})
    assert response.status_code == 201, response.text
    data = response.json()
    assert data['awaiting_review']
    assert data['state']['plan'] == ['parser', 'matching', 'semantic', 'human_review']
    result = data['state']['semantic_suggestions']
    assert result['status'] == 'current'
    assert result['criteria'][0]['candidates'][0]['excerpt'] == 'UniquePythonEvidenceForReview'
    assert 'semantic_refs' not in data['state']
    with sqlite3.connect(path) as db:
        checkpoints = db.execute('SELECT checkpoint FROM checkpoints').fetchall()
        assert checkpoints and all(b'UniquePythonEvidenceForReview' not in row[0] for row in checkpoints)
    loaded = client.get(f"/api/graph-reviews/{data['id']}")
    assert loaded.status_code == 200
    assert loaded.json()['state']['semantic_suggestions'] == result
    assert client.post(f"/api/graph-reviews/{data['id']}/resume", json={'reviewed': True}).status_code == 200
    assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
    assert client.get(f"/api/graph-reviews/{data['id']}").status_code == 403


@pytest.mark.parametrize('change', ['resume', 'criteria', 'criteria_reverted', 'owner_reverted'])
def test_changed_inputs_hide_suggestions_and_block_confirmation(scenario, change):
    client, path, application = scenario
    data = client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'semantic_review'}).json()
    with connect(path) as db:
        if change == 'resume':
            db.execute("UPDATE applications SET resume_text='Changed' WHERE id=?", (application,))
        elif change in ('criteria', 'criteria_reverted'):
            db.execute("UPDATE rubric_criteria SET label='Changed' WHERE job_id='job-backend-python'")
            if change == 'criteria_reverted':
                db.execute("UPDATE rubric_criteria SET label='Python' WHERE job_id='job-backend-python'")
        else:
            original = db.execute('SELECT candidate_id FROM applications WHERE id=?', (application,)).fetchone()[0]
            other = db.execute("SELECT id FROM users WHERE username='employee'").fetchone()[0]
            db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (other, application))
            db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (original, application))
    response = client.get(f"/api/graph-reviews/{data['id']}")
    assert response.status_code == 200
    assert response.json()['state']['semantic_suggestions']['status'] == 'stale'
    assert 'UniquePythonEvidenceForReview' not in response.text
    assert client.post(f"/api/graph-reviews/{data['id']}/resume", json={'reviewed': True}).status_code == 409


def test_missing_model_fails_explicitly(scenario, monkeypatch):
    client, _, application = scenario
    monkeypatch.delenv('TAWZEEF_ENABLE_SEMANTIC_SCREENING')
    response = client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'semantic_review'})
    assert response.status_code == 503
    assert 'not configured' in response.text


def test_changed_then_restored_inputs_during_model_call_are_rejected(scenario, monkeypatch):
    from app import semantic_screening

    client, path, application = scenario

    class Model:
        def encode(self, texts, *, query):
            if query:
                with connect(path) as db:
                    db.execute("UPDATE rubric_criteria SET label='Changed' WHERE job_id='job-backend-python'")
                    db.execute("UPDATE rubric_criteria SET label='Python' WHERE job_id='job-backend-python'")
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(semantic_screening, '_model', lambda _: Model())
    response = client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'semantic_review'})
    assert response.status_code == 409
    assert 'UniquePythonEvidenceForReview' not in response.text


@pytest.mark.skipif(os.environ.get('TAWZEEF_TEST_REAL_SEMANTIC') != '1', reason='Requires pinned local model')
def test_real_local_model_through_graph(tmp_path, monkeypatch):
    model_dir = Path(__file__).resolve().parents[2] / '.tooling' / 'multilingual-e5-small'
    monkeypatch.setenv('TAWZEEF_ENABLE_SEMANTIC_SCREENING', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', str(model_dir))
    path = tmp_path / 'real-model.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        with connect(path) as db:
            owner = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text,status)
                VALUES (?,'job-backend-python','synthetic.txt','Python development experience','submitted')""",
                (owner,)).lastrowid
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        assert client.post('/api/jobs/job-backend-python/rubric/criteria', json={
            'category': 'mandatory', 'label': 'Python development',
        }).status_code == 201
        response = client.post('/api/graph-reviews', json={'application_id': app_id, 'intent': 'semantic_review'})
        assert response.status_code == 201, response.text
        result = response.json()['state']['semantic_suggestions']
        assert result['status'] == 'current'
        assert any('Python' in item['excerpt'] for item in result['criteria'][0]['candidates'])

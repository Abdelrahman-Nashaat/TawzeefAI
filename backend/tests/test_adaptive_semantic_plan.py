"""Adaptive semantic planning remains explicitly enabled and source-bound."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import adaptive_planner, semantic_screening
from app.db import connect
from app.main import create_app


@pytest.fixture
def case(tmp_path, monkeypatch):
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    monkeypatch.setenv('TAWZEEF_ENABLE_SEMANTIC_SCREENING', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'local')

    class Model:
        def encode(self, texts, *, query):
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(semantic_screening, '_model', lambda _: Model())
    path = tmp_path / 'adaptive-semantic.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            owner = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text,status)
                VALUES (?,'job-backend-python','synthetic.txt','Skills\nSyntheticPythonEvidence','submitted')""", (owner,)).lastrowid
        assert client.post('/api/jobs/job-backend-python/rubric/criteria', json={
            'category': 'mandatory', 'label': 'Python'}).status_code == 201
        yield client, path, application


def start(client, application, allow=True):
    return client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'adaptive',
        'adaptive_goal': 'review_application', 'allow_semantic': allow})


def test_opted_in_semantic_is_source_bound_and_reviewed(case):
    client, path, application = case
    response = start(client, application)
    assert response.status_code == 201, response.text
    review = response.json()
    assert review['state']['plan'] == ['parser', 'matching', 'semantic', 'status', 'human_review']
    result = review['state']['semantic_suggestions']
    assert result['status'] == 'current'
    assert result['criteria'][0]['candidates'][0]['excerpt'] == 'SyntheticPythonEvidence'
    with sqlite3.connect(path) as db:
        checkpoints = db.execute('SELECT checkpoint FROM checkpoints').fetchall()
        assert checkpoints and all(b'SyntheticPythonEvidence' not in row[0] for row in checkpoints)
    assert client.get(f"/api/graph-reviews/{review['id']}").json()['state']['semantic_suggestions'] == result
    assert client.post(f"/api/graph-reviews/{review['id']}/resume", json={'reviewed': True}).status_code == 200


def test_stale_or_reverted_criteria_block_semantic_resume(case):
    client, path, application = case
    review = start(client, application).json()
    with connect(path) as db:
        db.execute("UPDATE rubric_criteria SET label='Changed' WHERE job_id='job-backend-python'")
        db.execute("UPDATE rubric_criteria SET label='Python' WHERE job_id='job-backend-python'")
    response = client.get(f"/api/graph-reviews/{review['id']}")
    assert response.status_code == 200
    assert response.json()['state']['semantic_suggestions']['status'] == 'stale'
    assert 'SyntheticPythonEvidence' not in response.text
    assert client.post(f"/api/graph-reviews/{review['id']}/resume", json={'reviewed': True}).status_code == 409


def test_disabled_or_unconfigured_does_not_require_model(case, monkeypatch):
    client, _, application = case
    disabled = start(client, application, allow=False)
    assert disabled.status_code == 201
    assert 'semantic' not in disabled.json()['state']['plan']
    monkeypatch.delenv('TAWZEEF_ENABLE_SEMANTIC_SCREENING')
    unavailable = start(client, application)
    assert unavailable.status_code == 201
    assert 'semantic' not in unavailable.json()['state']['plan']
    assert unavailable.json()['state']['plan_context']['semantic_available'] is False


def test_model_may_select_only_valid_enabled_semantic_plan(case, monkeypatch):
    client, _, application = case
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'test-key')
    captured = []

    def plan(facts, _key):
        captured.append(facts.copy())
        return {'steps': ['parser', 'matching', 'semantic', 'status']}

    monkeypatch.setattr(adaptive_planner, '_request_model', plan)
    response = start(client, application)
    assert response.status_code == 201, response.text
    assert response.json()['state']['plan_source'] == 'model'
    assert captured == [{'status': 'submitted', 'has_screening': False, 'semantic_available': True}]
    assert 'SyntheticPythonEvidence' not in str(captured)
    invalid = start(client, application, allow=False)
    assert invalid.status_code == 201
    assert invalid.json()['state']['plan_source'] == 'deterministic_fallback'
    assert 'semantic' not in invalid.json()['state']['plan']


def test_adaptive_semantic_schedule_requires_current_evidence_and_approval(case):
    client, path, application = case
    with connect(path) as db:
        db.execute("UPDATE applications SET status='advanced' WHERE id=?", (application,))
    start_time = datetime.now(UTC) + timedelta(days=4)
    payload = {'application_id': application, 'intent': 'adaptive', 'adaptive_goal': 'schedule_interview',
               'allow_semantic': True, 'slot': {'start_at': start_time.isoformat(),
               'end_at': (start_time + timedelta(minutes=30)).isoformat(), 'interviewer': 'Synthetic interviewer'}}
    response = client.post('/api/graph-reviews', json=payload)
    assert response.status_code == 201, response.text
    review = response.json()
    assert review['state']['semantic_suggestions']['status'] == 'current'
    assert review['state']['action_proposal']['availability_scope'] == 'all_eligible_candidates'
    fingerprint = review['state']['action_proposal']['approval_fingerprint']
    with connect(path) as db:
        db.execute("UPDATE rubric_criteria SET label='Changed' WHERE job_id='job-backend-python'")
        db.execute("UPDATE rubric_criteria SET label='Python' WHERE job_id='job-backend-python'")
    result = client.post(f"/api/graph-reviews/{review['id']}/resume", json={
        'reviewed': True, 'approval_fingerprint': fingerprint})
    assert result.status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0
    response = client.post('/api/graph-reviews', json=payload)
    assert response.status_code == 201, response.text
    review = response.json()
    result = client.post(f"/api/graph-reviews/{review['id']}/resume", json={
        'reviewed': True, 'approval_fingerprint': review['state']['action_proposal']['approval_fingerprint']})
    assert result.status_code == 200, result.text
    assert result.json()['state']['action_result']['status'] == 'created'
    assert client.post(f"/api/graph-reviews/{review['id']}/resume", json={
        'reviewed': True, 'approval_fingerprint': review['state']['action_proposal']['approval_fingerprint']}).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 1


@pytest.mark.parametrize('steps', [
    ['semantic', 'status'], ['matching', 'parser', 'semantic', 'status'],
    ['parser', 'semantic', 'matching', 'status'], ['parser', 'matching', 'semantic', 'status', 'semantic'],
])
def test_invalid_semantic_plans_are_rejected(steps):
    with pytest.raises(ValueError):
        adaptive_planner.validate_steps({'steps': steps}, {'status': 'submitted', 'has_screening': False,
                                                         'semantic_available': True})

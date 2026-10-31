import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import adaptive_planner, graph_orchestrator
from app.db import connect
from app.main import create_app


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    path = tmp_path / 'schedule.sqlite3'
    with TestClient(create_app(path, seed_demo=True), raise_server_exceptions=False) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application_id = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text,status)
                VALUES (?, 'job-backend-python', 'x.docx', 'synthetic', 'advanced')""", (candidate,)).lastrowid
        yield path, client, application_id


def request_body(application_id):
    start = datetime.now(UTC) + timedelta(days=4)
    return {'application_id': application_id, 'intent': 'adaptive', 'adaptive_goal': 'schedule_interview',
            'slot': {'start_at': start.isoformat(), 'end_at': (start + timedelta(minutes=30)).isoformat(),
                     'interviewer': 'Synthetic interviewer'}}


def start(client, application_id):
    response = client.post('/api/graph-reviews', json=request_body(application_id))
    assert response.status_code == 201, response.text
    return response.json()


def resume(client, review, *, reviewed=True, fingerprint=True):
    payload = {'reviewed': reviewed}
    if fingerprint:
        payload['approval_fingerprint'] = review['state']['action_proposal']['approval_fingerprint']
    return client.post(f"/api/graph-reviews/{review['id']}/resume", json=payload)


def counts(path):
    with connect(path) as db:
        return (db.execute('SELECT count(*) FROM interview_slots').fetchone()[0],
                db.execute('SELECT count(*) FROM slot_audit_events').fetchone()[0],
                db.execute('SELECT count(*) FROM interviews').fetchone()[0])


def test_adaptive_schedule_reviews_explicit_action_and_replay_is_safe(setup):
    path, client, application_id = setup
    review = start(client, application_id)
    state = review['state']
    assert state['plan'] == ['parser', 'matching', 'status', 'scheduler', 'prepare_slot', 'human_review', 'execute_slot']
    assert state['action_proposal']['availability_scope'] == 'all_eligible_candidates'
    assert 'context_digest' not in state and counts(path) == (0, 0, 0)
    reopened = client.get(f"/api/graph-reviews/{review['id']}").json()
    assert reopened['state']['action_proposal'] == state['action_proposal']
    assert resume(client, review, fingerprint=False).status_code == 409
    assert counts(path) == (0, 0, 0)
    result = resume(client, review)
    assert result.status_code == 200, result.text
    assert result.json()['state']['action_result']['status'] == 'created'
    assert resume(client, review).status_code == 409
    assert counts(path) == (1, 1, 0)


def test_decline_and_changed_application_do_not_create_slot(setup):
    path, client, application_id = setup
    declined = start(client, application_id)
    assert resume(client, declined, reviewed=False, fingerprint=False).json()['state']['action_result']['status'] == 'declined'
    changed = start(client, application_id)
    with connect(path) as db:
        db.execute("UPDATE applications SET resume_text='changed' WHERE id=?", (application_id,))
    assert resume(client, changed).status_code == 409
    assert counts(path) == (0, 0, 0)


def test_model_cannot_choose_action_or_receive_slot_details(setup, monkeypatch):
    path, client, application_id = setup
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'fake')
    captured = []
    def fake_model(facts, key):
        captured.append(facts)
        return {'steps': ['status', 'execute_slot'], 'candidate_id': 999}
    monkeypatch.setattr(adaptive_planner, '_request_model', fake_model)
    review = start(client, application_id)
    assert captured == [{'status': 'advanced', 'has_screening': False}]
    assert review['state']['plan_source'] == 'deterministic_fallback'
    assert review['state']['plan'][-3:] == ['prepare_slot', 'human_review', 'execute_slot']
    assert counts(path) == (0, 0, 0)
    invalid = request_body(application_id)
    invalid['candidate_id'] = 999
    assert client.post('/api/graph-reviews', json=invalid).status_code == 422


def test_conflict_and_concurrent_approval_are_safe(setup):
    path, client, application_id = setup
    review = start(client, application_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: resume(client, review).status_code, range(2)))
    assert sorted(results) == [200, 409]
    assert counts(path) == (1, 1, 0)
    conflict = start(client, application_id)
    assert resume(client, conflict).status_code == 409
    assert counts(path) == (1, 1, 0)


def test_goal_requires_valid_inputs_and_eligible_application(setup):
    path, client, application_id = setup
    missing = request_body(application_id)
    missing.pop('slot')
    assert client.post('/api/graph-reviews', json=missing).status_code == 422
    wrong = request_body(application_id)
    wrong['adaptive_goal'] = 'review_application'
    assert client.post('/api/graph-reviews', json=wrong).status_code == 422
    with connect(path) as db:
        db.execute("UPDATE applications SET status='rejected' WHERE id=?", (application_id,))
    assert client.post('/api/graph-reviews', json=request_body(application_id)).status_code == 409
    assert counts(path) == (0, 0, 0)


def test_expired_session_and_crash_roll_back_action_and_checkpoint(setup, monkeypatch):
    path, client, application_id = setup
    review = start(client, application_id)
    times = iter([time.time(), time.time() + 365 * 24 * 60 * 60])
    with monkeypatch.context() as patch:
        patch.setattr(graph_orchestrator, 'time', SimpleNamespace(time=lambda: next(times)))
        assert resume(client, review).status_code == 401
    assert counts(path) == (0, 0, 0)
    assert client.get(f"/api/graph-reviews/{review['id']}").json()['awaiting_review']
    original = graph_orchestrator.create_slot_record
    def fail(*args):
        original(*args)
        raise RuntimeError('synthetic failure after insert')
    with monkeypatch.context() as patch:
        patch.setattr(graph_orchestrator, 'create_slot_record', fail)
        assert resume(client, review).status_code == 500
    assert counts(path) == (0, 0, 0)
    assert client.get(f"/api/graph-reviews/{review['id']}").json()['awaiting_review']
    assert resume(client, review).status_code == 200
    assert counts(path) == (1, 1, 0)

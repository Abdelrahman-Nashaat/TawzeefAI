import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import graph_orchestrator as graph
from app.db import connect
from app.main import create_app


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / 'action.sqlite3'
    with TestClient(create_app(path, seed_demo=True), raise_server_exceptions=False) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?, 'job-backend-python', 'x.docx', 'synthetic', 'advanced')", (candidate,)).lastrowid
        yield path, client, app_id


def proposal(client, app_id):
    start = datetime.now(UTC) + timedelta(days=3)
    response = client.post('/api/graph-reviews', json={'application_id': app_id, 'intent': 'create_slot',
        'slot': {'start_at': start.isoformat(), 'end_at': (start + timedelta(minutes=30)).isoformat(), 'interviewer': 'Interview Owner'}})
    assert response.status_code == 201, response.text
    return response.json()


def resume(client, review, reviewed=True, fingerprint=True):
    body = {'reviewed': reviewed}
    if fingerprint:
        body['approval_fingerprint'] = review['state']['action_proposal']['approval_fingerprint']
    return client.post(f"/api/graph-reviews/{review['id']}/resume", json=body)


def counts(path):
    with connect(path) as db:
        return (db.execute('SELECT count(*) FROM interview_slots').fetchone()[0],
                db.execute('SELECT count(*) FROM slot_audit_events').fetchone()[0],
                db.execute('SELECT count(*) FROM interviews').fetchone()[0])


def test_approval_is_explicit_and_replay_safe(setup):
    path, client, app_id = setup
    review = proposal(client, app_id)
    assert review['awaiting_review']
    action = review['state']['action_proposal']
    assert action['availability_scope'] == 'all_eligible_candidates'
    assert 'not reserved for this application' in action['notice']
    restored = client.get(f"/api/graph-reviews/{review['id']}")
    assert restored.status_code == 200
    assert restored.json()['state']['action_proposal'] == action
    assert resume(client, review, fingerprint=False).status_code == 409
    assert client.post(f"/api/graph-reviews/{review['id']}/resume", json={'reviewed': True, 'approval_fingerprint': 'bad'}).status_code == 409
    assert client.post(f"/api/graph-reviews/{review['id']}/resume", json={'reviewed': True, 'approval_fingerprint': 'مخالف'}).status_code == 409
    assert counts(path) == (0, 0, 0)
    result = resume(client, review)
    assert result.status_code == 200, result.text
    assert result.json()['state']['action_result']['status'] == 'created'
    assert resume(client, review).status_code == 409
    assert counts(path) == (1, 1, 0)


def test_decline_does_not_create_slot(setup):
    path, client, app_id = setup
    review = proposal(client, app_id)
    result = resume(client, review, reviewed=False, fingerprint=False)
    assert result.status_code == 200
    assert result.json()['state']['action_result'] == {'status': 'declined'}
    assert counts(path) == (0, 0, 0)


def test_stale_application_or_conflicting_slot_cannot_be_approved(setup):
    path, client, app_id = setup
    review = proposal(client, app_id)
    with connect(path) as db:
        db.execute("UPDATE applications SET status='rejected' WHERE id=?", (app_id,))
    assert resume(client, review).status_code == 409
    assert counts(path) == (0, 0, 0)
    with connect(path) as db:
        db.execute("UPDATE applications SET status='advanced' WHERE id=?", (app_id,))
    other = proposal(client, app_id)
    assert client.post('/api/slots', json=other['state']['action_proposal']['slot']).status_code == 201
    assert resume(client, other).status_code == 409
    assert counts(path) == (1, 1, 0)


def test_expired_session_before_commit_rolls_back_action(setup, monkeypatch):
    path, client, app_id = setup
    review = proposal(client, app_id)
    times = iter([time.time(), time.time() + 60 * 60 * 24 * 365])
    with monkeypatch.context() as patch:
        patch.setattr(graph, 'time', SimpleNamespace(time=lambda: next(times)))
        assert resume(client, review).status_code == 401
    assert counts(path) == (0, 0, 0)
    assert client.get(f"/api/graph-reviews/{review['id']}").json()['awaiting_review']
    assert resume(client, review).status_code == 200
    assert counts(path) == (1, 1, 0)


def test_failure_after_insert_rolls_back_and_retry_succeeds(setup, monkeypatch):
    path, client, app_id = setup
    review = proposal(client, app_id)
    original = graph.create_slot_record
    def fail(*args):
        original(*args)
        raise RuntimeError('simulated crash')
    with monkeypatch.context() as patch:
        patch.setattr(graph, 'create_slot_record', fail)
        assert resume(client, review).status_code == 500
    assert counts(path) == (0, 0, 0)
    assert client.get(f"/api/graph-reviews/{review['id']}").json()['awaiting_review']
    assert resume(client, review).status_code == 200
    assert counts(path) == (1, 1, 0)


def test_simultaneous_approvals_create_at_most_one_slot(setup):
    path, client, app_id = setup
    review = proposal(client, app_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: resume(client, review).status_code, range(2)))
    assert sorted(results) == [200, 409]
    assert counts(path) == (1, 1, 0)

"""The bounded adaptive planner must never bypass review or atomicity."""

import pytest
from fastapi import HTTPException

from app.db import connect

pytest_plugins = ('test_graph_advance_reserve',)


def start(client, application, slot):
    response = client.post('/api/graph-reviews', json={
        'application_id': application, 'intent': 'adaptive',
        'adaptive_goal': 'advance_and_reserve', 'slot': slot,
    })
    assert response.status_code == 201, response.text
    review = response.json()
    proposal = review['state']['action_proposal']
    assert proposal['type'] == 'advance_and_reserve_slot'
    assert proposal['availability_scope'] == 'selected_application_only'
    assert review['awaiting_review']
    return review['id'], proposal['approval_fingerprint'], review['state']['plan']


def test_adaptive_advance_requires_approval_and_is_replay_safe(scenario):
    client, path, application, candidate, slot = scenario
    review, fingerprint, plan = start(client, application, slot)
    assert plan.index('parser') < plan.index('matching') < plan.index('prepare_advance')
    assert 'status' in plan
    endpoint = f'/api/graph-reviews/{review}/resume'
    assert client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': 'bad'}).status_code == 409
    result = client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': fingerprint})
    assert result.status_code == 200, result.text
    assert result.json()['state']['action_result']['status'] == 'advanced_and_reserved'
    assert client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': fingerprint}).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'advanced'
        assert tuple(db.execute('SELECT target_application_id,target_candidate_id FROM interview_slots').fetchone()) == (application, candidate)


def test_conflict_rolls_back_both_actions(scenario):
    client, path, application, _, slot = scenario
    review, fingerprint, _ = start(client, application, slot)
    assert client.post('/api/slots', json=slot).status_code == 201
    response = client.post(f'/api/graph-reviews/{review}/resume', json={'reviewed': True, 'approval_fingerprint': fingerprint})
    assert response.status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (application,)).fetchone()[0] == 0


def test_changed_then_restored_assessment_or_owner_cannot_approve(scenario):
    client, path, application, _, slot = scenario
    review, fingerprint, _ = start(client, application, slot)
    with connect(path) as db:
        db.execute("UPDATE rubric_evidence SET excerpt='changed' WHERE application_id=?", (application,))
        db.execute("UPDATE rubric_evidence SET excerpt='Python' WHERE application_id=?", (application,))
    response = client.post(f'/api/graph-reviews/{review}/resume', json={'reviewed': True, 'approval_fingerprint': fingerprint})
    assert response.status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0


def test_declined_proposal_never_advances_or_reserves(scenario):
    client, path, application, _, slot = scenario
    review, _, _ = start(client, application, slot)
    response = client.post(f'/api/graph-reviews/{review}/resume', json={'reviewed': False})
    assert response.status_code == 200
    assert response.json()['state']['action_result']['status'] == 'declined'
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0


def test_expired_authorization_rolls_back_action_and_checkpoint(scenario, monkeypatch):
    from app import graph_orchestrator

    client, path, application, _, slot = scenario
    review, fingerprint, _ = start(client, application, slot)
    original = graph_orchestrator._authorize_resume
    calls = 0

    def authorize(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(status_code=401, detail='Session expired')
        return original(*args)

    monkeypatch.setattr(graph_orchestrator, '_authorize_resume', authorize)
    endpoint = f'/api/graph-reviews/{review}/resume'
    assert client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': fingerprint}).status_code == 401
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (application,)).fetchone()[0] == 0
    monkeypatch.setattr(graph_orchestrator, '_authorize_resume', original)
    assert client.post(endpoint, json={'reviewed': True, 'approval_fingerprint': fingerprint}).status_code == 200


def test_opted_in_semantic_evidence_is_reviewed_and_stale_blocks_approval(scenario, monkeypatch):
    from app import semantic_screening

    class Model:
        def encode(self, texts, *, query):
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setenv('TAWZEEF_ENABLE_SEMANTIC_SCREENING', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'local')
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    monkeypatch.setattr(semantic_screening, '_model', lambda _: Model())
    client, path, application, _, slot = scenario
    response = client.post('/api/graph-reviews', json={'application_id': application, 'intent': 'adaptive',
        'adaptive_goal': 'advance_and_reserve', 'allow_semantic': True, 'slot': slot})
    assert response.status_code == 201, response.text
    review = response.json()
    assert 'semantic' in review['state']['plan']
    assert review['state']['semantic_suggestions']['status'] == 'current'
    with connect(path) as db:
        db.execute("UPDATE rubric_criteria SET label='Changed' WHERE job_id='job-backend-python'")
        db.execute("UPDATE rubric_criteria SET label='Python' WHERE job_id='job-backend-python'")
    view = client.get(f"/api/graph-reviews/{review['id']}")
    assert view.status_code == 200
    assert view.json()['state']['semantic_suggestions']['status'] == 'stale'
    result = client.post(f"/api/graph-reviews/{review['id']}/resume", json={
        'reviewed': True, 'approval_fingerprint': review['state']['action_proposal']['approval_fingerprint']})
    assert result.status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0


@pytest.mark.parametrize('steps', [
    ['status'],
    ['matching', 'parser', 'status'],
    ['parser', 'matching', 'scheduler', 'status'],
    ['parser', 'matching', 'status', 'execute_advance'],
])
def test_untrusted_planner_cannot_omit_or_inject_steps(steps):
    from app.adaptive_planner import validate_steps

    with pytest.raises(ValueError):
        validate_steps({'steps': steps}, {'goal': 'advance_and_reserve', 'status': 'submitted',
                                          'has_screening': True, 'semantic_available': False})

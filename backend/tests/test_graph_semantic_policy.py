"""Semantic evidence and policy citations in supervised recruitment reviews."""
import sqlite3

import pytest
from fastapi import HTTPException

from app.db import connect

pytest_plugins = ('test_graph_semantic_advance_reserve',)


@pytest.mark.parametrize('intent', ['semantic_review', 'semantic_advance_and_reserve_slot'])
def test_semantic_policy_review_respects_current_sources(case, intent):
    client, path, application, _, slot = case
    policy = client.post('/api/knowledge/documents/text', json={
        'title': 'Quasar policy', 'audience': 'hr', 'collection': 'handbook',
        'text': 'Quasar leave is eleven days. private-policy-example',
    }).json()['id']
    body = {'application_id': application, 'intent': intent,
            'policy_question': 'What is quasar leave?'}
    if intent == 'semantic_advance_and_reserve_slot':
        body['slot'] = slot
    response = client.post('/api/graph-reviews', json=body)
    assert response.status_code == 201, response.text
    review = response.json()
    assert review['state']['policy']['status'] == 'answer'
    assert review['state']['semantic_suggestions']['status'] == 'current'
    with sqlite3.connect(path) as db:
        checkpoints = db.execute('SELECT checkpoint FROM checkpoints').fetchall()
        assert checkpoints
        assert all(b'What is quasar leave?' not in row[0] and b'private-policy-example' not in row[0]
                   for row in checkpoints)
    with connect(path) as db:
        db.execute('UPDATE knowledge_documents SET active=0 WHERE id=?', (policy,))
    payload = {'reviewed': True}
    if intent == 'semantic_advance_and_reserve_slot':
        payload['approval_fingerprint'] = review['state']['action_proposal']['approval_fingerprint']
    assert client.post(f"/api/graph-reviews/{review['id']}/resume", json=payload).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0
    assert client.post(f"/api/graph-reviews/{review['id']}/resume", json={'reviewed': False}).status_code == 200


def test_combined_review_approves_once_and_reserves_private_slot(case):
    client, path, application, candidate, slot = case
    assert client.post('/api/knowledge/documents/text', json={
        'title': 'Quasar policy', 'audience': 'hr', 'collection': 'handbook',
        'text': 'Quasar leave is eleven days.',
    }).status_code == 201
    response = client.post('/api/graph-reviews', json={
        'application_id': application, 'intent': 'semantic_advance_and_reserve_slot',
        'slot': slot, 'policy_question': 'What is quasar leave?',
    })
    assert response.status_code == 201, response.text
    review = response.json()
    assert review['state']['policy']['status'] == 'answer'
    assert review['state']['semantic_suggestions']['status'] == 'current'
    result = client.post(f"/api/graph-reviews/{review['id']}/resume", json={
        'reviewed': True, 'approval_fingerprint': review['state']['action_proposal']['approval_fingerprint'],
    })
    assert result.status_code == 200, result.text
    slot_id = result.json()['state']['action_result']['slot']['id']
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'advanced'
        assert tuple(db.execute('SELECT target_application_id,target_candidate_id FROM interview_slots WHERE id=?',
                                (slot_id,)).fetchone()) == (application, candidate)


@pytest.mark.parametrize('change,expected', [('resume', 409), ('role', 403)])
def test_combined_review_rechecks_resume_and_hr_role(case, change, expected):
    client, path, application, _, slot = case
    assert client.post('/api/knowledge/documents/text', json={
        'title': 'Quasar policy', 'audience': 'hr', 'collection': 'handbook',
        'text': 'Quasar leave is eleven days.',
    }).status_code == 201
    response = client.post('/api/graph-reviews', json={
        'application_id': application, 'intent': 'semantic_advance_and_reserve_slot',
        'slot': slot, 'policy_question': 'What is quasar leave?',
    })
    assert response.status_code == 201, response.text
    review = response.json()
    with connect(path) as db:
        if change == 'resume':
            db.execute("UPDATE applications SET resume_text='changed' WHERE id=?", (application,))
        else:
            db.execute("UPDATE users SET role='employee' WHERE username='hr'")
    response = client.post(f"/api/graph-reviews/{review['id']}/resume", json={
        'reviewed': True, 'approval_fingerprint': review['state']['action_proposal']['approval_fingerprint'],
    })
    assert response.status_code == expected
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0


def test_combined_policy_check_holds_write_lock_and_expiry_rolls_back(case, monkeypatch):
    from app import graph_orchestrator

    client, path, application, _, slot = case
    policy = client.post('/api/knowledge/documents/text', json={
        'title': 'Quasar policy', 'audience': 'hr', 'collection': 'handbook',
        'text': 'Quasar leave is eleven days.',
    }).json()['id']
    response = client.post('/api/graph-reviews', json={
        'application_id': application, 'intent': 'semantic_advance_and_reserve_slot',
        'slot': slot, 'policy_question': 'What is quasar leave?',
    })
    assert response.status_code == 201, response.text
    review = response.json()
    original_policy = graph_orchestrator._adaptive_policy_current
    original_auth = graph_orchestrator._authorize_resume
    calls = 0

    def policy_under_lock(db, value):
        assert db.in_transaction
        with sqlite3.connect(str(path), timeout=0) as other, pytest.raises(sqlite3.OperationalError, match='locked'):
            other.execute('UPDATE knowledge_documents SET active=0 WHERE id=?', (policy,))
        return original_policy(db, value)

    def expire_before_commit(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(status_code=401, detail='Session expired')
        return original_auth(*args)

    monkeypatch.setattr(graph_orchestrator, '_adaptive_policy_current', policy_under_lock)
    monkeypatch.setattr(graph_orchestrator, '_authorize_resume', expire_before_commit)
    endpoint = f"/api/graph-reviews/{review['id']}/resume"
    payload = {'reviewed': True, 'approval_fingerprint': review['state']['action_proposal']['approval_fingerprint']}
    assert client.post(endpoint, json=payload).status_code == 401
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0
    monkeypatch.setattr(graph_orchestrator, '_authorize_resume', original_auth)
    assert client.post(endpoint, json=payload).status_code == 200

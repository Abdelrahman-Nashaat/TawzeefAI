"""Policy evidence remains current when HR reviews offer actions."""
import sqlite3

import pytest
from fastapi import HTTPException
from langgraph.checkpoint.sqlite import SqliteSaver

from app.db import connect
from app.graph_orchestrator import _config, build_review_graph

pytest_plugins = ('test_graph_offer',)


@pytest.mark.parametrize('intent,params', [
    ('present_offer', {'offer_details': 'Synthetic terms'}),
    ('reject_application', {'rejection_reason': 'Synthetic reason'}),
])
def test_policy_requires_current_source_and_allows_discard(case, intent, params):
    client, path, application, _, _, _ = case
    policy = client.post('/api/knowledge/documents/text', json={
        'title': 'Quasar policy', 'audience': 'hr', 'collection': 'handbook',
        'text': 'Quasar leave is eleven days. private-policy-example',
    }).json()['id']
    response = client.post('/api/graph-reviews', json={
        'intent': intent, 'application_id': application,
        'policy_question': 'What is quasar leave?', **params,
    })
    assert response.status_code == 201, response.text
    review = response.json()
    assert review['state']['policy']['status'] == 'answer'
    assert 'private-policy-example' in review['state']['policy']['answer']
    with SqliteSaver.from_conn_string(str(path)) as saver:
        history = list(build_review_graph(path, saver).get_state_history(_config(review['id'])))
    assert 'What is quasar leave?' not in str(history)
    assert 'private-policy-example' not in str(history)
    with connect(path) as db:
        db.execute('UPDATE knowledge_documents SET active=0 WHERE id=?', (policy,))
    approve = client.post(f"/api/graph-reviews/{review['id']}/resume", json={
        'reviewed': True, 'approval_fingerprint': review['state']['action_proposal']['approval_fingerprint'],
    })
    assert approve.status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM job_offers').fetchone()[0] == 0
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'advanced'
    assert client.post(f"/api/graph-reviews/{review['id']}/resume", json={'reviewed': False}).status_code == 200


@pytest.mark.parametrize('intent,params', [
    ('present_offer', {'offer_details': 'Synthetic terms'}),
    ('reject_application', {'rejection_reason': 'Synthetic reason'}),
])
def test_policy_and_hr_action_commit_together(case, intent, params):
    client, path, application, _, _, _ = case
    response = client.post('/api/knowledge/documents/text', json={
        'title': 'Quasar policy', 'audience': 'hr', 'collection': 'handbook',
        'text': 'Quasar leave is eleven days.',
    })
    assert response.status_code == 201
    response = client.post('/api/graph-reviews', json={
        'intent': intent, 'application_id': application, 'policy_question': 'What is quasar leave?', **params,
    })
    assert response.status_code == 201, response.text
    review = response.json()
    assert review['state']['policy']['status'] == 'answer'
    result = client.post(f"/api/graph-reviews/{review['id']}/resume", json={
        'reviewed': True, 'approval_fingerprint': review['state']['action_proposal']['approval_fingerprint'],
    })
    assert result.status_code == 200, result.text
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == (
            'offered' if intent == 'present_offer' else 'rejected')


def test_policy_checked_under_write_lock_and_expiry_rolls_back(case, monkeypatch):
    from app import graph_orchestrator

    client, path, application, _, _, _ = case
    policy = client.post('/api/knowledge/documents/text', json={
        'title': 'Quasar policy', 'audience': 'hr', 'collection': 'handbook',
        'text': 'Quasar leave is eleven days.',
    }).json()['id']
    response = client.post('/api/graph-reviews', json={
        'intent': 'present_offer', 'application_id': application,
        'offer_details': 'Synthetic terms', 'policy_question': 'What is quasar leave?',
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
    body = {'reviewed': True, 'approval_fingerprint': review['state']['action_proposal']['approval_fingerprint']}
    assert client.post(f"/api/graph-reviews/{review['id']}/resume", json=body).status_code == 401
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM job_offers').fetchone()[0] == 0
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'advanced'
    monkeypatch.setattr(graph_orchestrator, '_authorize_resume', original_auth)
    assert client.post(f"/api/graph-reviews/{review['id']}/resume", json=body).status_code == 200

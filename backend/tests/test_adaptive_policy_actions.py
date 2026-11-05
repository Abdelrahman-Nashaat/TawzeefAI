import sqlite3

import pytest
from fastapi import HTTPException
from langgraph.checkpoint.sqlite import SqliteSaver

from app.db import connect
from app.graph_orchestrator import _config, build_review_graph

pytest_plugins = ('test_graph_advance_reserve', 'test_adaptive_schedule')


def add_policy(client):
    response = client.post('/api/knowledge/documents/text', json={
        'title': 'Quasar leave', 'audience': 'hr', 'collection': 'handbook',
        'text': 'Quasar leave is eleven days. unique-policy-marker',
    })
    assert response.status_code == 201, response.text
    return response.json()['id']


def begin(client, application, goal, slot):
    response = client.post('/api/graph-reviews', json={
        'application_id': application, 'intent': 'adaptive', 'adaptive_goal': goal,
        'slot': slot, 'policy_question': 'What is quasar leave?',
    })
    assert response.status_code == 201, response.text
    review = response.json()
    assert review['state']['policy']['status'] == 'answer'
    assert 'unique-policy-marker' in review['state']['policy']['answer']
    assert 'policy' in review['state']['plan']
    assert review['awaiting_review']
    return review


def approve(client, review, decision=True):
    return client.post(f"/api/graph-reviews/{review['id']}/resume", json={
        'reviewed': decision,
        'approval_fingerprint': review['state']['action_proposal']['approval_fingerprint'],
    })


def test_advance_and_reserve_with_policy_requires_current_source(scenario):
    client, path, application, _, slot = scenario
    document = add_policy(client)
    review = begin(client, application, 'advance_and_reserve', slot)
    assert review['state']['plan'].index('matching') < review['state']['plan'].index('policy')
    with SqliteSaver.from_conn_string(str(path)) as saver:
        history = list(build_review_graph(path, saver).get_state_history(_config(review['id'])))
    assert 'What is quasar leave?' not in str(history)
    assert 'unique-policy-marker' not in str(history)
    with connect(path) as db:
        db.execute("UPDATE knowledge_documents SET collection='job_description' WHERE id=?", (document,))
    result = approve(client, review)
    assert result.status_code == 409, result.text
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (application,)).fetchone()[0] == 0
    assert approve(client, review, False).status_code == 200


def test_advance_and_reserve_with_current_policy_succeeds(scenario):
    client, path, application, _, slot = scenario
    add_policy(client)
    review = begin(client, application, 'advance_and_reserve', slot)
    assert approve(client, review).status_code == 200
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'advanced'
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 1


def test_schedule_with_policy_rechecks_source_and_approval(setup):
    from test_adaptive_schedule import request_body
    path, client, application = setup
    document = add_policy(client)
    body = request_body(application)
    review = begin(client, application, 'schedule_interview', body['slot'])
    with connect(path) as db:
        db.execute("UPDATE knowledge_documents SET active=0 WHERE id=?", (document,))
    assert approve(client, review).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0
    assert approve(client, review, False).status_code == 200


def test_approval_checks_policy_with_write_lock_and_expiry_rolls_back(scenario, monkeypatch):
    from app import graph_orchestrator

    client, path, application, _, slot = scenario
    document = add_policy(client)
    review = begin(client, application, 'advance_and_reserve', slot)
    original_policy = graph_orchestrator._adaptive_policy_current
    original_auth = graph_orchestrator._authorize_resume
    calls = 0

    def policy_under_lock(db, policy):
        assert db.in_transaction
        with sqlite3.connect(str(path), timeout=0) as other, pytest.raises(sqlite3.OperationalError, match='locked'):
            other.execute('UPDATE knowledge_documents SET active=0 WHERE id=?', (document,))
        return original_policy(db, policy)

    def expire_before_commit(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise HTTPException(status_code=401, detail='Session expired')
        return original_auth(*args)

    monkeypatch.setattr(graph_orchestrator, '_adaptive_policy_current', policy_under_lock)
    monkeypatch.setattr(graph_orchestrator, '_authorize_resume', expire_before_commit)
    assert approve(client, review).status_code == 401
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'
        assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='decision'", (application,)).fetchone()[0] == 0
    monkeypatch.setattr(graph_orchestrator, '_authorize_resume', original_auth)
    assert approve(client, review).status_code == 200

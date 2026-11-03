"""Policy answer eligibility must not rely only on a document title."""

import json

import pytest
from fastapi.testclient import TestClient

from app import policy_agent
from app.auth import User
from app.db import connect
from app.main import create_app


@pytest.fixture
def policy(tmp_path):
    path = tmp_path / 'title-policy.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        with connect(path) as db:
            db.execute('UPDATE knowledge_settings SET include_sample_policies=0 WHERE id=1')
            users = {row['username']: User(row['id'], row['username'], row['display_name'], row['role'])
                     for row in db.execute('SELECT * FROM users')}
            doc_id = db.execute('''INSERT INTO knowledge_documents(title,audience,text,created_by,collection)
                VALUES ('Annual Leave Policy','staff','Parking permits are issued on Fridays.',?,'handbook')''',
                (users['hr'].id,)).lastrowid
        yield path, client, users, doc_id


def test_title_only_match_is_an_unverified_lead_not_an_answer(policy):
    path, _, users, _ = policy
    result = policy_agent.run_policy_agent(path, users['employee'], 'What is annual leave?')
    assert result.answer.mode == 'abstain'
    assert 'not an answer' in result.answer.answer
    assert 'unverified' in result.answer.notice
    assert 'title' in result.answer.notice.lower()
    assert [source.text for source in result.answer.sources] == ['Parking permits are issued on Fridays.']
    candidate = policy_agent.run_policy_agent(path, users['candidate'], 'What is annual leave?')
    assert candidate.answer.mode == 'abstain' and not candidate.answer.sources


def test_matching_body_can_still_be_answered(policy):
    path, _, users, doc_id = policy
    with connect(path) as db:
        db.execute("UPDATE knowledge_documents SET text='Annual leave provides twenty days.' WHERE id=?", (doc_id,))
    result = policy_agent.run_policy_agent(path, users['employee'], 'What is annual leave?')
    assert result.answer.mode == 'extractive' and len(result.answer.sources) == 1


def test_title_only_lead_rechecks_access(policy):
    path, _, users, doc_id = policy
    calls = 0

    def authorize():
        nonlocal calls
        calls += 1
        if calls == 2:
            with connect(path) as db:
                db.execute("UPDATE knowledge_documents SET audience='hr' WHERE id=?", (doc_id,))

    with pytest.raises(PermissionError):
        policy_agent.run_policy_agent(path, users['employee'], 'What is annual leave?', authorize=authorize)


def test_model_and_hybrid_do_not_promote_title_only(policy, monkeypatch):
    path, _, users, _ = policy
    with connect(path) as db:
        source = policy_agent.authorized_policy_sources(db, 'employee')[0]
    row = {'id': source.id, 'title': source.title, 'text': source.text, 'page': source.page,
           'section': source.section, 'collection': 'handbook'}
    monkeypatch.setenv('GROQ_API_KEY', 'dummy')
    monkeypatch.setenv('TAWZEEF_POLICY_HYBRID_ANSWERS', '1')
    monkeypatch.setattr(policy_agent, '_request_search', lambda *_: {'tool_calls': [{
        'type': 'function', 'function': {'name': 'search_policy', 'arguments': json.dumps({'query': 'annual leave'})},
    }]})
    monkeypatch.setattr(policy_agent, '_policy_hybrid', lambda *_: {'sources': [row]})
    monkeypatch.setattr(policy_agent, '_request_quote_selection', lambda *_: pytest.fail('Title-only body sent for answer'))
    result = policy_agent.run_policy_agent(path, users['employee'], 'What is annual leave?',
                                           enabled=True, allow_hybrid_answers=True)
    assert result.answer.mode == 'abstain' and 'not an answer' in result.answer.answer


def test_lead_disappears_from_saved_conversation_after_access_revocation(policy):
    path, client, _, doc_id = policy
    assert client.post('/api/auth/login', json={'username': 'employee', 'password': 'employee-demo-only'}).status_code == 200
    conversation = client.post('/api/conversations', json={'title': 'Policy'}).json()['id']
    response = client.post(f'/api/conversations/{conversation}/messages',
                           json={'question': 'What is annual leave?', 'mode': 'policy'})
    assert response.status_code == 201 and response.json()['mode'] == 'abstain'
    assert response.json()['sources']
    with connect(path) as db:
        db.execute("UPDATE knowledge_documents SET audience='hr' WHERE id=?", (doc_id,))
    stored = client.get(f'/api/conversations/{conversation}').json()['messages'][0]
    assert stored['mode'] == 'unavailable' and stored['sources'] == []
    assert 'next_step' not in stored
    assert 'Parking permits' not in stored['answer']

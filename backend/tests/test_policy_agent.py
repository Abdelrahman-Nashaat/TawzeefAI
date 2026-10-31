import json

import pytest
from fastapi.testclient import TestClient

from app import policy_agent
from app.auth import User
from app.db import connect
from app.main import create_app


@pytest.fixture
def policy_setup(tmp_path):
    path = tmp_path / 'policy.sqlite3'
    with TestClient(create_app(path, seed_demo=True)):
        with connect(path) as db:
            users = {row['username']: User(row['id'], row['username'], row['display_name'], row['role'])
                     for row in db.execute('SELECT * FROM users')}
            hr = users['hr']
            def add(title, audience, text, collection):
                return db.execute('''INSERT INTO knowledge_documents(title,audience,text,created_by,collection)
                    VALUES (?,?,?,?,?)''', (title, audience, text, hr.id, collection)).lastrowid
            staff = add('Annual leave', 'staff', 'Annual leave provides twenty paid days.', 'handbook')
            add('HR secret', 'hr', 'Annual leave provides confidential manager information.', 'handbook')
            add('Job description', 'staff', 'Python SQL developer job requirements.', 'job_description')
            add('Old data', 'staff', 'Python SQL legacy document.', 'legacy_unclassified')
            add('Previous hires', 'hr', 'Python SQL successful candidate patterns.', 'past_hire_patterns')
        yield path, users, staff


def tool(query):
    return {'tool_calls': [{'type': 'function', 'function': {
        'name': 'search_policy', 'arguments': json.dumps({'query': query}),
    }}]}


def test_search_filters_role_and_collection_before_ranking(policy_setup):
    path, users, _ = policy_setup
    result = policy_agent.run_policy_agent(path, users['hr'], 'What is the Python SQL policy?')
    assert result.answer.mode == 'abstain'
    employee = policy_agent.run_policy_agent(path, users['employee'], 'annual leave')
    assert employee.answer.mode == 'extractive'
    assert any(s.title == 'Annual leave' for s in employee.answer.sources)
    assert all(s.title != 'HR secret' for s in employee.answer.sources)
    candidate = policy_agent.run_policy_agent(path, users['candidate'], 'annual leave')
    assert all(s.title not in {'Annual leave', 'HR secret'} for s in candidate.answer.sources)


def test_distinctive_missing_qualifier_abstains(policy_setup):
    path, users, _ = policy_setup
    result = policy_agent.run_policy_agent(path, users['employee'], 'parental annual leave')
    assert result.answer.mode == 'abstain'
    assert not result.answer.sources


def test_model_tool_and_citations_are_validated(policy_setup, monkeypatch):
    path, users, _ = policy_setup
    monkeypatch.setenv('GROQ_API_KEY', 'test-key')
    monkeypatch.setattr(policy_agent, '_request_search', lambda q, k: tool('annual leave'))
    def answer(question, passages):
        return 'Verify the entitlement [' + passages[0]['id'] + ']'
    monkeypatch.setattr(policy_agent, 'generate_with_groq', answer)
    result = policy_agent.run_policy_agent(path, users['employee'], 'What is annual leave?', enabled=True)
    assert result.answer.mode == 'groq'
    assert result.tool_trace == ['search_policy']
    assert result.answer.sources
    monkeypatch.setattr(policy_agent, 'generate_with_groq', lambda *_: 'Fake [not-a-source]')
    result = policy_agent.run_policy_agent(path, users['employee'], 'What is annual leave?', enabled=True)
    assert result.answer.mode == 'extractive'
    assert 'Fake' not in result.answer.answer


def test_invalid_tool_and_added_search_words_fall_back_locally(policy_setup, monkeypatch):
    path, users, _ = policy_setup
    monkeypatch.setenv('GROQ_API_KEY', 'test-key')
    monkeypatch.setattr(policy_agent, '_request_search', lambda q, k: tool('annual leave salary'))
    monkeypatch.setattr(policy_agent, 'generate_with_groq', lambda *_: 'Invented [fake]')
    result = policy_agent.run_policy_agent(path, users['employee'], 'annual leave', enabled=True)
    assert result.answer.mode == 'extractive'
    assert all('salary' not in source.text.lower() for source in result.answer.sources)
    monkeypatch.setattr(policy_agent, '_request_search', lambda q, k: {'tool_calls': [{'type': 'function', 'function': {'name': 'send_email', 'arguments': '{}'}}]})
    result = policy_agent.run_policy_agent(path, users['employee'], 'annual leave', enabled=True)
    assert result.answer.mode == 'extractive'


def test_model_abstention_never_returns_unverified_text(policy_setup, monkeypatch):
    path, users, _ = policy_setup
    monkeypatch.setenv('GROQ_API_KEY', 'test-key')
    monkeypatch.setattr(policy_agent, '_request_search', lambda q, k: {'content': 'invented answer'})
    result = policy_agent.run_policy_agent(path, users['employee'], 'annual leave', enabled=True)
    assert result.answer.mode == 'abstain'
    assert 'invented' not in result.answer.answer
    assert not result.tool_trace


def test_revoked_or_reclassified_source_cannot_be_returned(policy_setup, monkeypatch):
    path, users, staff = policy_setup
    monkeypatch.setenv('GROQ_API_KEY', 'test-key')
    monkeypatch.setattr(policy_agent, '_request_search', lambda q, k: tool('annual leave'))
    def revoke(question, passages):
        with connect(path) as db:
            db.execute("UPDATE knowledge_documents SET collection='job_description' WHERE id=?", (staff,))
        return 'Entitlement [' + passages[0]['id'] + ']'
    monkeypatch.setattr(policy_agent, 'generate_with_groq', revoke)
    with pytest.raises(PermissionError):
        policy_agent.run_policy_agent(path, users['employee'], 'annual leave', enabled=True)


def test_session_rechecked_before_sending(policy_setup, monkeypatch):
    path, users, _ = policy_setup
    monkeypatch.setenv('GROQ_API_KEY', 'test-key')
    calls = []
    def check():
        calls.append(1)
        if len(calls) == 2:
            raise PermissionError('revoked')
    monkeypatch.setattr(policy_agent, '_request_search', lambda q, k: tool('annual leave'))
    monkeypatch.setattr(policy_agent, 'generate_with_groq', lambda *_: pytest.fail('unauthorized send'))
    with pytest.raises(PermissionError):
        policy_agent.run_policy_agent(path, users['employee'], 'annual leave', enabled=True, authorize=check)

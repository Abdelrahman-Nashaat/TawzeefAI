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


def test_eligibility_cannot_be_inferred_from_a_policy_about_requests(policy_setup):
    path, users, staff = policy_setup
    with connect(path) as db:
        db.execute('UPDATE knowledge_settings SET include_sample_policies=0 WHERE id=1')
        db.execute("UPDATE knowledge_documents SET text='Annual leave requests are recorded in the HR portal.' WHERE id=?", (staff,))
    result = policy_agent.run_policy_agent(path, users['employee'], 'Who is eligible for annual leave?')
    assert result.answer.mode == 'abstain'
    assert result.answer.sources == []
    with connect(path) as db:
        db.execute("UPDATE knowledge_documents SET text='Employees are eligible for annual leave after probation.' WHERE id=?", (staff,))
    result = policy_agent.run_policy_agent(path, users['employee'], 'Who is eligible for annual leave?')
    assert result.answer.mode == 'extractive'
    assert any('eligible' in source.text for source in result.answer.sources)


def test_model_tool_and_citations_are_validated(policy_setup, monkeypatch):
    path, users, _ = policy_setup
    monkeypatch.setenv('GROQ_API_KEY', 'test-key')
    monkeypatch.setattr(policy_agent, '_request_search', lambda q, k: tool('annual leave'))
    def answer(question, passages):
        return json.dumps({'quotes': [{'source_id': passages[0]['id'], 'quote': passages[0]['text']}]})
    monkeypatch.setattr(policy_agent, '_request_quote_selection', answer)
    result = policy_agent.run_policy_agent(path, users['employee'], 'What is annual leave?', enabled=True)
    assert result.answer.mode == 'groq'
    assert result.tool_trace == ['search_policy']
    assert result.answer.sources
    monkeypatch.setattr(policy_agent, '_request_quote_selection', lambda *_: 'Fake [not-a-source]')
    result = policy_agent.run_policy_agent(path, users['employee'], 'What is annual leave?', enabled=True)
    assert result.answer.mode == 'extractive'
    assert 'Fake' not in result.answer.answer


def test_invalid_tool_and_added_search_words_fall_back_locally(policy_setup, monkeypatch):
    path, users, _ = policy_setup
    monkeypatch.setenv('GROQ_API_KEY', 'test-key')
    monkeypatch.setattr(policy_agent, '_request_search', lambda q, k: tool('annual leave salary'))
    monkeypatch.setattr(policy_agent, '_request_quote_selection', lambda *_: 'Invented [fake]')
    result = policy_agent.run_policy_agent(path, users['employee'], 'annual leave', enabled=True)
    assert result.answer.mode == 'extractive'
    assert all('salary' not in source.text.lower() for source in result.answer.sources)
    monkeypatch.setattr(policy_agent, '_request_search', lambda q, k: {'tool_calls': [{'type': 'function', 'function': {'name': 'send_email', 'arguments': '{}'}}]})
    result = policy_agent.run_policy_agent(path, users['employee'], 'annual leave', enabled=True)
    assert result.answer.mode == 'extractive'


def test_model_cannot_attach_a_real_source_id_to_invented_entitlement(policy_setup, monkeypatch):
    path, users, _ = policy_setup
    monkeypatch.setenv('GROQ_API_KEY', 'test-key')
    monkeypatch.setattr(policy_agent, '_request_search', lambda q, k: tool('annual leave'))
    monkeypatch.setattr(policy_agent, '_request_quote_selection', lambda q, sources: json.dumps({
        'quotes': [{'source_id': sources[0]['id'], 'quote': 'Annual leave provides unlimited paid days.'}],
    }))
    result = policy_agent.run_policy_agent(path, users['employee'], 'annual leave', enabled=True)
    assert result.answer.mode == 'extractive'
    assert 'unlimited' not in result.answer.answer
    assert 'could not be verified' in result.answer.notice


def test_model_empty_selection_causes_abstention(policy_setup, monkeypatch):
    path, users, _ = policy_setup
    monkeypatch.setenv('GROQ_API_KEY', 'test-key')
    monkeypatch.setattr(policy_agent, '_request_search', lambda q, k: tool('annual leave'))
    monkeypatch.setattr(policy_agent, '_request_quote_selection', lambda *_: '{"quotes":[]}')
    result = policy_agent.run_policy_agent(path, users['employee'], 'annual leave', enabled=True)
    assert result.answer.mode == 'abstain'
    assert result.answer.sources == []


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
    monkeypatch.setattr(policy_agent, '_request_quote_selection', revoke)
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
    monkeypatch.setattr(policy_agent, '_request_quote_selection', lambda *_: pytest.fail('unauthorized send'))
    with pytest.raises(PermissionError):
        policy_agent.run_policy_agent(path, users['employee'], 'annual leave', enabled=True, authorize=check)


def test_hybrid_reorders_only_current_authorized_lexical_candidates(policy_setup, monkeypatch):
    path, users, staff = policy_setup
    with connect(path) as db:
        db.execute('UPDATE knowledge_settings SET include_sample_policies=0 WHERE id=1')
        second = db.execute("""INSERT INTO knowledge_documents(title,audience,text,created_by,collection)
            VALUES ('Annual leave policy','staff','Annual leave may be paid monthly.',?,'handbook')""", (users['hr'].id,)).lastrowid
        sources = policy_agent.authorized_policy_sources(db, 'employee')
    selected = next(s for s in sources if s.id.startswith(f'db-doc-{second}-'))
    first = next(s for s in sources if s.id.startswith(f'db-doc-{staff}-'))
    row = lambda s: {'id': s.id, 'title': s.title, 'text': s.text, 'page': s.page,
                     'section': s.section, 'collection': 'handbook'}
    monkeypatch.setenv('TAWZEEF_USE_POLICY_HYBRID', '1')
    monkeypatch.setattr(policy_agent, '_policy_hybrid', lambda *args: {'sources': [row(selected),
        {**row(first), 'text': 'forged secret'}, {**row(first), 'collection': 'job_description'}]})
    result = policy_agent.run_policy_agent(path, users['employee'], 'annual leave')
    assert result.answer.sources[0] == selected
    assert all(s.text != 'forged secret' for s in result.answer.sources)
    assert 'reorders eligible lexical matches only' in result.answer.notice
    assert policy_agent.run_policy_agent(path, users['employee'], 'parental annual leave').answer.mode == 'abstain'


def test_hybrid_failure_falls_back_and_revocation_fails_closed(policy_setup, monkeypatch):
    path, users, staff = policy_setup
    monkeypatch.setenv('TAWZEEF_USE_POLICY_HYBRID', '1')
    def fail(*args):
        raise RuntimeError('sensitive connection string')
    monkeypatch.setattr(policy_agent, '_policy_hybrid', fail)
    result = policy_agent.run_policy_agent(path, users['employee'], 'annual leave')
    assert result.answer.sources
    assert 'hybrid unavailable' in result.answer.notice
    assert 'sensitive connection string' not in result.answer.notice
    def revoke(*args):
        with connect(path) as db:
            db.execute("UPDATE knowledge_documents SET collection='job_description' WHERE id=?", (staff,))
        return {'sources': []}
    monkeypatch.setattr(policy_agent, '_policy_hybrid', revoke)
    with pytest.raises(PermissionError):
        policy_agent.run_policy_agent(path, users['employee'], 'annual leave')


def test_hybrid_enforces_collection_at_search_boundary(policy_setup, monkeypatch):
    from contextlib import nullcontext

    import psycopg

    from app import hybrid_api, postgres_retrieval

    path, users, _ = policy_setup
    seen = {}
    def search(path_arg, role, question, model, connection, **kwargs):
        seen.update(role=role, question=question, collection=kwargs.get('collection'))
        return {'sources': []}
    monkeypatch.setenv('TAWZEEF_ENABLE_HYBRID', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'fake-model')
    monkeypatch.setattr(hybrid_api, '_load_model', lambda _: object())
    monkeypatch.setattr(psycopg, 'connect', lambda **_: nullcontext(object()))
    monkeypatch.setattr(postgres_retrieval, 'search_postgres', search)
    policy_agent._policy_hybrid(path, users['employee'].role, 'annual leave')
    assert seen == {'role': 'employee', 'question': 'annual leave', 'collection': 'handbook'}

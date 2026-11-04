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


@pytest.mark.parametrize('contraction', ['aren\u02bct', 'aren\u2018t', 'aren\uff07t', "shan't", "needn't", "mightn't"])
def test_negated_policy_contractions_are_not_returned_as_an_answer(policy_setup, contraction):
    path, users, _ = policy_setup
    with connect(path) as db:
        db.execute('''INSERT INTO knowledge_documents(title,audience,text,created_by,collection)
            VALUES (?,?,?,?,?)''', ('Travel reimbursement', 'staff',
                                   f'Employees {contraction} eligible for travel reimbursement.',
                                   users['hr'].id, 'handbook'))
    result = policy_agent.run_policy_agent(path, users['employee'], 'travel reimbursement')
    assert result.answer.mode == 'abstain'
    assert result.answer.sources == []
    assert 'Negation' in result.answer.notice


@pytest.mark.parametrize('contraction', ['aren\u02bct', 'aren\u2018t', 'aren\uff07t', "shan't", "needn't", "mightn't"])
def test_negated_questions_with_contractions_do_not_return_positive_policy(policy_setup, contraction):
    path, users, _ = policy_setup
    result = policy_agent.run_policy_agent(path, users['employee'], f'Why {contraction} annual leave available?')
    assert result.answer.mode == 'abstain'
    assert result.answer.sources == []
    assert 'Negation' in result.answer.notice


@pytest.mark.parametrize('negator', ['\u0648\u0644\u0627', '\u0641\u0644\u0627', '\u0648\u0644\u0645', '\u0641\u0644\u0646', '\u0648\u0644\u064a\u0633', '\u0648\u064e\u0644\u064e\u0627'])
def test_prefixed_arabic_negation_is_not_returned_as_supported_answer(policy_setup, negator):
    path, users, _ = policy_setup
    with connect(path) as db:
        db.execute('''INSERT INTO knowledge_documents(title,audience,text,created_by,collection)
            VALUES (?,?,?,?,?)''', ('Travel reimbursement', 'staff',
                                  f'Travel reimbursement {negator} eligible for staff.',
                                  users['hr'].id, 'handbook'))
    result = policy_agent.run_policy_agent(path, users['employee'], 'travel reimbursement')
    assert result.answer.mode == 'abstain'
    assert result.answer.sources == []
    assert 'Negation' in result.answer.notice


@pytest.mark.parametrize('negator', ['\u0648\u0644\u0627', '\u0641\u0644\u0627', '\u0648\u0644\u0645', '\u0641\u0644\u0646', '\u0648\u0644\u064a\u0633', '\u0648\u064e\u0644\u064e\u0627'])
def test_prefixed_arabic_negation_in_question_does_not_return_positive_policy(policy_setup, negator):
    path, users, _ = policy_setup
    result = policy_agent.run_policy_agent(path, users['employee'], f'Annual leave, {negator} available?')
    assert result.answer.mode == 'abstain'
    assert result.answer.sources == []
    assert 'Negation' in result.answer.notice


@pytest.mark.parametrize('ordinary_word', ['\u0648\u0644\u0627\u062f\u0629', '\u0648\u0644\u0627\u064a\u0629', '\u0641\u0644\u0627\u062d\u0629'])
def test_arabic_words_with_same_prefix_are_not_negators(policy_setup, ordinary_word):
    path, users, _ = policy_setup
    result = policy_agent.run_policy_agent(path, users['employee'], f'Annual leave {ordinary_word}')
    assert not policy_agent._has_explicit_negation(ordinary_word)
    assert 'Negation' not in (result.answer.notice or '')


def test_search_rate_limit_falls_back_without_another_model_call(policy_setup, monkeypatch):
    path, users, _ = policy_setup
    monkeypatch.setenv('GROQ_API_KEY', 'private-test-key')
    monkeypatch.setattr(policy_agent, '_request_search', lambda *_: (_ for _ in ()).throw(
        policy_agent.ProviderRateLimited('private-test-key')))
    monkeypatch.setattr(policy_agent, '_request_quote_selection', lambda *_: pytest.fail('Model called again'))
    result = policy_agent.run_policy_agent(path, users['employee'], 'annual leave', enabled=True)
    assert result.answer.mode == 'extractive'
    assert 'rate limited' in result.answer.notice
    assert 'private-test-key' not in result.answer.notice


def test_selection_rate_limit_preserves_extract_and_reports_rate_limit(policy_setup, monkeypatch):
    path, users, _ = policy_setup
    monkeypatch.setenv('GROQ_API_KEY', 'private-test-key')
    monkeypatch.setattr(policy_agent, '_request_search', lambda *_: tool('annual leave'))
    monkeypatch.setattr(policy_agent, '_request_quote_selection', lambda *_: (_ for _ in ()).throw(
        policy_agent.ProviderRateLimited('private-test-key')))
    result = policy_agent.run_policy_agent(path, users['employee'], 'annual leave', enabled=True)
    assert result.answer.mode == 'extractive'
    assert 'rate limited' in result.answer.notice
    assert 'private-test-key' not in result.answer.notice


def test_rate_limited_fallback_still_rechecks_authorization(policy_setup, monkeypatch):
    path, users, _ = policy_setup
    monkeypatch.setenv('GROQ_API_KEY', 'private-test-key')
    monkeypatch.setattr(policy_agent, '_request_search', lambda *_: (_ for _ in ()).throw(
        policy_agent.ProviderRateLimited('rate limited')))
    checks = []
    def authorize():
        checks.append(True)
        if len(checks) > 1:
            raise PermissionError('access revoked')
    with pytest.raises(PermissionError):
        policy_agent.run_policy_agent(path, users['employee'], 'annual leave', enabled=True, authorize=authorize)


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


def test_lexical_model_answer_only_returns_selected_citations(policy_setup, monkeypatch):
    path, users, staff = policy_setup
    with connect(path) as db:
        db.execute('UPDATE knowledge_settings SET include_sample_policies=0 WHERE id=1')
        other = db.execute('''INSERT INTO knowledge_documents(title,audience,text,created_by,collection)
            VALUES (?,?,?,?,?)''', ('Annual leave rules', 'staff',
            'Annual leave requests require manager approval.', users['hr'].id, 'handbook')).lastrowid
    monkeypatch.setenv('GROQ_API_KEY', 'test-key')
    monkeypatch.setattr(policy_agent, '_request_search', lambda *_: tool('annual leave'))
    calls = []
    def select(_question, passages):
        calls.extend(passages)
        selected = next(item for item in passages if item['id'].startswith(f'db-doc-{staff}-'))
        return json.dumps({'quotes': [{'source_id': selected['id'], 'quote': selected['text']}]})
    monkeypatch.setattr(policy_agent, '_request_quote_selection', select)
    result = policy_agent.run_policy_agent(path, users['employee'], 'annual leave', enabled=True)
    assert {item['id'].split('-p')[0] for item in calls} == {f'db-doc-{staff}', f'db-doc-{other}'}
    assert result.answer.mode == 'groq'
    assert [source.id for source in result.answer.sources] == [next(item['id'] for item in calls if item['id'].startswith(f'db-doc-{staff}-'))]
    assert 'manager approval' not in result.answer.answer


def test_repeated_quotes_from_one_source_yield_one_citation(policy_setup, monkeypatch):
    path, users, staff = policy_setup
    with connect(path) as db:
        db.execute('UPDATE knowledge_settings SET include_sample_policies=0 WHERE id=1')
        db.execute("UPDATE knowledge_documents SET text=? WHERE id=?",
                   ('Annual leave starts in January.\nAnnual leave is approved by managers.', staff))
    monkeypatch.setenv('GROQ_API_KEY', 'test-key')
    monkeypatch.setattr(policy_agent, '_request_search', lambda *_: tool('annual leave'))
    def select(_question, passages):
        source = next(item for item in passages if item['id'].startswith(f'db-doc-{staff}-'))
        return json.dumps({'quotes': [{'source_id': source['id'], 'quote': line} for line in source['text'].splitlines()]})
    monkeypatch.setattr(policy_agent, '_request_quote_selection', select)
    result = policy_agent.run_policy_agent(path, users['employee'], 'annual leave', enabled=True)
    assert result.answer.mode == 'groq'
    assert [source.id for source in result.answer.sources] == [f'db-doc-{staff}-p1']
    assert result.answer.answer.count(f'[db-doc-{staff}-p1]') == 2


def test_selection_keeps_only_the_selected_page_of_a_document(policy_setup, monkeypatch):
    path, users, staff = policy_setup
    pages = [{'page': 1, 'text': 'Annual leave accrues twenty days.'},
             {'page': 2, 'text': 'Annual leave requests require approval.'}]
    with connect(path) as db:
        db.execute('UPDATE knowledge_settings SET include_sample_policies=0 WHERE id=1')
        db.execute('UPDATE knowledge_documents SET text=?,pages_json=? WHERE id=?',
                   ('\n'.join(page['text'] for page in pages), json.dumps(pages), staff))
    monkeypatch.setenv('GROQ_API_KEY', 'test-key')
    monkeypatch.setattr(policy_agent, '_request_search', lambda *_: tool('annual leave'))
    seen = []
    def select(_question, passages):
        seen.extend(passages)
        selected = next(item for item in passages if item['id'] == f'db-doc-{staff}-p2')
        return json.dumps({'quotes': [{'source_id': selected['id'], 'quote': selected['text']}]})
    monkeypatch.setattr(policy_agent, '_request_quote_selection', select)
    result = policy_agent.run_policy_agent(path, users['employee'], 'annual leave', enabled=True)
    assert {item['id'] for item in seen} == {f'db-doc-{staff}-p1', f'db-doc-{staff}-p2'}
    assert [(source.id, source.page) for source in result.answer.sources] == [(f'db-doc-{staff}-p2', 2)]
    assert 'twenty days' not in result.answer.answer


def test_selected_lexical_citation_is_preserved_and_reauthorized_on_replay(policy_setup, monkeypatch):
    path, users, selected_id = policy_setup
    with connect(path) as db:
        db.execute('UPDATE knowledge_settings SET include_sample_policies=0 WHERE id=1')
        other_id = db.execute('''INSERT INTO knowledge_documents(title,audience,text,created_by,collection)
            VALUES (?,?,?,?,?)''', ('Annual leave rules', 'staff',
            'Annual leave requests require manager approval.', users['hr'].id, 'handbook')).lastrowid
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'test-only')
    monkeypatch.setattr(policy_agent, '_request_search', lambda *_: tool('annual leave'))
    def select(_question, passages):
        selected = next(item for item in passages if item['id'].startswith(f'db-doc-{selected_id}-'))
        return json.dumps({'quotes': [{'source_id': selected['id'], 'quote': selected['text']}]})
    monkeypatch.setattr(policy_agent, '_request_quote_selection', select)
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'employee', 'password': 'employee-demo-only'}).status_code == 200
        conversation = client.post('/api/conversations', json={'title': 'Policy'}).json()['id']
        response = client.post(f'/api/conversations/{conversation}/messages', json={'question': 'annual leave', 'mode': 'policy'})
        assert response.status_code == 201
        message = response.json()
        assert message['mode'] == 'groq'
        assert [source['id'] for source in message['sources']] == [f'db-doc-{selected_id}-p1']
        assert 'manager approval' not in json.dumps(message, ensure_ascii=False)
        with connect(path) as db:
            db.execute("UPDATE knowledge_documents SET audience='hr' WHERE id=?", (other_id,))
        replay = client.get(f'/api/conversations/{conversation}').json()['messages'][0]
        assert replay['mode'] == 'groq'
        assert [source['id'] for source in replay['sources']] == [f'db-doc-{selected_id}-p1']
        with connect(path) as db:
            db.execute("UPDATE knowledge_documents SET audience='hr' WHERE id=?", (selected_id,))
        replay = client.get(f'/api/conversations/{conversation}').json()['messages'][0]
        assert replay['mode'] == 'unavailable' and replay['sources'] == []
        assert 'twenty paid days' not in replay['answer']


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

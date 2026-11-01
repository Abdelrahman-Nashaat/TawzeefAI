"""Hybrid search offers unverified leads only after the policy agent abstains."""
import pytest
from fastapi.testclient import TestClient

from app import policy_agent
from app.auth import User
from app.db import connect
from app.main import create_app


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / 'suggestions.sqlite3'
    with TestClient(create_app(path, seed_demo=True)):
        with connect(path) as db:
            db.execute('UPDATE knowledge_settings SET include_sample_policies=0 WHERE id=1')
            rows = db.execute('SELECT * FROM users').fetchall()
            users = {r['username']: User(r['id'], r['username'], r['display_name'], r['role']) for r in rows}
            def add(title, audience, text, collection='handbook'):
                return db.execute('''INSERT INTO knowledge_documents(title,audience,text,created_by,collection)
                    VALUES (?,?,?,?,?)''', (title, audience, text, users['hr'].id, collection)).lastrowid
            accessible = add('Remote work', 'staff', 'Staff may work remotely two days per week.')
            secret = add('Private work', 'hr', 'Staff may work remotely five days per week.')
            other = add('Job description', 'staff', 'Staff may work remotely three days per week.', 'job_description')
            sources = policy_agent.authorized_policy_sources(db, 'hr')
            def row(doc_id):
                source = next(s for s in sources if s.id.startswith(f'db-doc-{doc_id}-'))
                return {'id': source.id, 'title': source.title, 'text': source.text, 'page': source.page,
                        'section': source.section, 'collection': 'handbook'}
            values = path, users, accessible, row(accessible), row(secret), other
        yield values


def test_suggestions_require_explicit_opt_in_and_never_become_an_answer(setup, monkeypatch):
    path, users, _, source, secret, _ = setup
    calls = []
    monkeypatch.delenv('TAWZEEF_USE_POLICY_HYBRID', raising=False)
    monkeypatch.delenv('TAWZEEF_POLICY_HYBRID_SUGGESTIONS', raising=False)
    monkeypatch.setattr(policy_agent, '_policy_hybrid', lambda *args: calls.append(args) or {'sources': [source]})
    baseline = policy_agent.run_policy_agent(path, users['employee'], 'Can employees work from home?')
    assert baseline.answer.mode == 'abstain' and baseline.answer.sources == [] and not calls

    monkeypatch.setenv('TAWZEEF_POLICY_HYBRID_SUGGESTIONS', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'dummy')
    monkeypatch.setattr(policy_agent, '_request_search', lambda *_: {'tool_calls': [{'type': 'function', 'function': {
        'name': 'search_policy', 'arguments': '{"query":"work from home"}'}}]})
    monkeypatch.setattr(policy_agent, 'generate_with_groq', lambda *_: pytest.fail('Unverified suggestions sent to model'))
    monkeypatch.setattr(policy_agent, '_policy_hybrid', lambda *_: {'sources': [secret, source,
        {**source, 'text': 'forged'}, {**source, 'collection': 'job_description'}, source]})
    result = policy_agent.run_policy_agent(path, users['employee'], 'Can employees work from home?', enabled=True)
    assert result.answer.mode == 'abstain'
    assert [s.id for s in result.answer.sources] == [source['id']]
    assert 'not an answer' in result.answer.answer
    assert 'unverified' in result.answer.notice


def test_mismatched_near_miss_is_not_presented_as_a_supported_answer(setup, monkeypatch):
    path, users, _, source, _, _ = setup
    monkeypatch.setenv('TAWZEEF_POLICY_HYBRID_SUGGESTIONS', '1')
    monkeypatch.setattr(policy_agent, '_policy_hybrid', lambda *_: {'sources': [source]})
    result = policy_agent.run_policy_agent(path, users['employee'], 'Does remote work include paid internet access?')
    assert result.answer.mode == 'abstain'
    assert 'not an answer' in result.answer.answer
    assert result.answer.sources and 'internet' not in result.answer.sources[0].text


def test_revoked_sources_cannot_be_suggested_and_failures_abstain(setup, monkeypatch):
    path, users, doc_id, source, _, _ = setup
    monkeypatch.setenv('TAWZEEF_POLICY_HYBRID_SUGGESTIONS', '1')
    def revoke(*_):
        with connect(path) as db:
            db.execute("UPDATE knowledge_documents SET audience='hr' WHERE id=?", (doc_id,))
        return {'sources': [source]}
    monkeypatch.setattr(policy_agent, '_policy_hybrid', revoke)
    result = policy_agent.run_policy_agent(path, users['employee'], 'Can employees work from home?')
    assert result.answer.mode == 'abstain' and result.answer.sources == []
    def fail(*_):
        raise RuntimeError('sensitive database credential')
    monkeypatch.setattr(policy_agent, '_policy_hybrid', fail)
    result = policy_agent.run_policy_agent(path, users['employee'], 'Can employees work from home?')
    assert result.answer.mode == 'abstain' and result.answer.sources == []
    assert 'sensitive database credential' not in result.answer.notice


def test_conversation_hides_previously_suggested_source_after_revocation(tmp_path, monkeypatch):
    path = tmp_path / 'conversation.sqlite3'
    monkeypatch.setenv('TAWZEEF_POLICY_HYBRID_SUGGESTIONS', '1')
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        response = client.post('/api/knowledge/documents/text', json={'title': 'Remote work',
            'text': 'Staff may work remotely two days per week.', 'audience': 'staff', 'collection': 'handbook'})
        assert response.status_code == 201
        doc_id = response.json()['id']
        with connect(path) as db:
            source = next(s for s in policy_agent.authorized_policy_sources(db, 'employee')
                          if s.id.startswith(f'db-doc-{doc_id}-'))
        row = {'id': source.id, 'title': source.title, 'text': source.text, 'page': source.page,
               'section': source.section, 'collection': 'handbook'}
        monkeypatch.setattr(policy_agent, '_policy_hybrid', lambda *_: {'sources': [row]})
        assert client.post('/api/auth/login', json={'username': 'employee', 'password': 'employee-demo-only'}).status_code == 200
        conversation = client.post('/api/conversations', json={'title': 'Policy'}).json()['id']
        message = client.post(f'/api/conversations/{conversation}/messages',
            json={'question': 'Can employees work from home?', 'mode': 'policy'})
        assert message.status_code == 201 and message.json()['mode'] == 'abstain'
        assert message.json()['sources'][0]['id'] == source.id
        with connect(path) as db:
            db.execute("UPDATE knowledge_documents SET audience='hr' WHERE id=?", (doc_id,))
        entry = client.get(f'/api/conversations/{conversation}').json()['messages'][0]
        assert entry['mode'] == 'unavailable' and entry['sources'] == []
        assert source.text not in entry['answer']

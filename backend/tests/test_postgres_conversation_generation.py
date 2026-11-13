"""Generated conversation answers require current owner, sources and consent."""
import pytest
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401

from app.main import create_app


def prepare(monkeypatch, hr, candidate):
    monkeypatch.setenv('TAWZEEF_POSTGRES_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'synthetic-key')
    login(hr, 'hr')
    login(candidate, 'candidate')
    public = hr.post('/api/knowledge/documents/text', json={'title': 'Annual leave',
        'text': 'Annual leave requires manager approval.', 'collection': 'handbook', 'audience': 'public', 'page': 3}).json()['id']
    hr.post('/api/knowledge/documents/text', json={'title': 'Private annual leave',
        'text': 'Annual leave secret exception.', 'collection': 'handbook', 'audience': 'hr'})
    return public, candidate.post('/api/knowledge/conversations').json()['id']


def test_generated_turn_is_explicit_role_scoped_and_revocable(mode, monkeypatch):  # noqa: F811
    import app.postgres_knowledge as knowledge
    seen = []
    def model(question, passages, *, previous_question):
        seen.append((question, passages, previous_question))
        return f"Ask your manager 【{passages[0]['id']}】."
    monkeypatch.setattr(knowledge, 'generate_with_groq', model)
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        doc, cid = prepare(monkeypatch, hr, candidate)
        url = f'/api/knowledge/conversations/{cid}/turns'
        assert candidate.post(url, json={'question': 'annual leave'}).status_code == 201
        assert not seen
        result = candidate.post(url, json={'question': 'annual leave approval', 'generate_answer': True}).json()
        assert result['mode'] == 'groq' and result['answer'] == f'Ask your manager [pg-doc-{doc}].'
        assert seen[0][2] == 'annual leave' and len(seen[0][1]) == 1
        assert 'secret exception' not in repr(seen)
        result = candidate.get(url).json()['items'][-1]
        assert result['mode'] == 'groq' and result['answer']
        assert hr.post(f'/api/knowledge/documents/{doc}/deactivate').status_code == 200
        result = candidate.get(url).json()['items'][-1]
        assert result['mode'] == 'source_unavailable' and result['answer'] is None and result['sources'] == []


@pytest.mark.parametrize('raw', ['Uncited statement.', 'Bad [unknown].'])
def test_invalid_model_output_is_not_saved(mode, monkeypatch, raw):  # noqa: F811
    import app.postgres_knowledge as knowledge
    monkeypatch.setattr(knowledge, 'generate_with_groq', lambda *a, **k: raw)
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        _, cid = prepare(monkeypatch, hr, candidate)
        url = f'/api/knowledge/conversations/{cid}/turns'
        response = candidate.post(url, json={'question': 'annual leave', 'generate_answer': True})
        assert response.status_code == 201 and response.json()['mode'] == 'extractive'
        assert candidate.get(url).json()['items'][0]['answer'] is None


def test_source_revoked_during_provider_call_returns_no_answer_or_turn(mode, monkeypatch):  # noqa: F811
    import app.postgres_knowledge as knowledge
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        doc, cid = prepare(monkeypatch, hr, candidate)
        def revoke(_question, passages, *, previous_question):
            assert hr.post(f'/api/knowledge/documents/{doc}/deactivate').status_code == 200
            return f"Ask your manager [{passages[0]['id']}]."
        monkeypatch.setattr(knowledge, 'generate_with_groq', revoke)
        url = f'/api/knowledge/conversations/{cid}/turns'
        assert candidate.post(url, json={'question': 'annual leave', 'generate_answer': True}).status_code == 409
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM knowledge_conversation_turns').fetchone()['n'] == 0


def test_session_expiry_after_provider_call_rolls_back(mode, monkeypatch):  # noqa: F811
    import time
    from types import SimpleNamespace

    import app.postgres_experimental as pg_app
    import app.postgres_knowledge as knowledge
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        _, cid = prepare(monkeypatch, hr, candidate)
        def expire(_question, passages, *, previous_question):
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 86400))
            return f"Ask your manager [{passages[0]['id']}]."
        monkeypatch.setattr(knowledge, 'generate_with_groq', expire)
        url = f'/api/knowledge/conversations/{cid}/turns'
        assert candidate.post(url, json={'question': 'annual leave', 'generate_answer': True}).status_code == 401
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM knowledge_conversation_turns').fetchone()['n'] == 0


def test_concurrent_turn_invalidates_provider_response(mode, monkeypatch):  # noqa: F811
    import app.postgres_knowledge as knowledge
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        _, cid = prepare(monkeypatch, hr, candidate)
        url = f'/api/knowledge/conversations/{cid}/turns'
        def interleave(_question, passages, *, previous_question):
            assert candidate.post(url, json={'question': 'annual leave'}).status_code == 201
            return f"Ask your manager [{passages[0]['id']}]."
        monkeypatch.setattr(knowledge, 'generate_with_groq', interleave)
        assert candidate.post(url, json={'question': 'annual leave approval', 'generate_answer': True}).status_code == 409
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM knowledge_conversation_turns').fetchone()['n'] == 1

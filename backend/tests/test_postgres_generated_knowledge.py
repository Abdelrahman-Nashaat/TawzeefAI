"""Opt-in, role-filtered generation for synthetic PostgreSQL documents."""
import pytest
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401

from app.ai_provider import ProviderUnavailable
from app.main import create_app


def setup_documents(hr):
    for title, text, audience in [
        ('Public leave', 'Annual leave requires manager approval.', 'public'),
        ('Private leave', 'Annual leave has a confidential exception.', 'hr'),
    ]:
        response = hr.post('/api/knowledge/documents/text', json={'title': title, 'text': text,
            'audience': audience, 'collection': 'handbook', 'page': 4})
        assert response.status_code == 201


def test_explicit_opt_in_and_role_filter(mode, monkeypatch):  # noqa: F811
    import app.postgres_knowledge as knowledge

    sent = []
    def fake_model(question, passages):
        sent.append((question, passages))
        return f"Ask your manager [{passages[0]['id']}]."
    monkeypatch.setattr(knowledge, 'generate_with_groq', fake_model)
    monkeypatch.setenv('TAWZEEF_POSTGRES_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'synthetic-test-only')
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        login(hr, 'hr'); login(candidate, 'candidate'); setup_documents(hr)
        response = candidate.post('/api/knowledge/answer', json={'question': 'annual leave'})
        assert response.json()['mode'] == 'extractive' and not sent
        response = candidate.post('/api/knowledge/answer', json={'question': 'annual leave', 'generate_answer': True})
        result = response.json()
        assert result['mode'] == 'groq' and 'Ask your manager' in result['answer']
        assert [s['title'] for s in result['sources']] == ['Public leave']
        assert result['sources'][0]['page'] == 4
        assert [p['title'] for p in sent[0][1]] == ['Public leave']
        assert 'confidential exception' not in repr(sent)
        assert 'do not establish factual accuracy' in result['notice']
        assert candidate.post('/api/knowledge/answer', json={'question': 'zebra galaxies',
                              'generate_answer': True}).json()['mode'] == 'abstain'
        assert len(sent) == 1


@pytest.mark.parametrize('bad_answer', ['A claim without a citation', 'A claim [unknown-source]'])
def test_invalid_citations_and_provider_failure_return_excerpts(mode, monkeypatch, bad_answer):  # noqa: F811
    import app.postgres_knowledge as knowledge

    monkeypatch.setenv('TAWZEEF_POSTGRES_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'synthetic-test-only')
    monkeypatch.setattr(knowledge, 'generate_with_groq', lambda *_: bad_answer)
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        login(hr, 'hr'); login(candidate, 'candidate'); setup_documents(hr)
        result = candidate.post('/api/knowledge/answer', json={'question': 'annual leave', 'generate_answer': True}).json()
        assert result['mode'] == 'extractive' and result['answer'] is None and result['sources']
        assert 'could not be verified' in result['notice']
        def unavailable(*_):
            raise ProviderUnavailable('secret failure details')
        monkeypatch.setattr(knowledge, 'generate_with_groq', unavailable)
        result = candidate.post('/api/knowledge/answer', json={'question': 'annual leave', 'generate_answer': True}).json()
        assert result['mode'] == 'extractive' and result['answer'] is None
        assert 'secret failure details' not in repr(result)


def test_session_expiry_after_provider_does_not_disclose_answer(mode, monkeypatch):  # noqa: F811
    import time
    from types import SimpleNamespace

    import app.postgres_experimental as pg_app
    import app.postgres_knowledge as knowledge

    monkeypatch.setenv('TAWZEEF_POSTGRES_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'synthetic-test-only')
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        login(hr, 'hr'); login(candidate, 'candidate'); setup_documents(hr)
        def expire(_question, passages):
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 86400))
            return f"Generated secret [{passages[0]['id']}]"
        monkeypatch.setattr(knowledge, 'generate_with_groq', expire)
        response = candidate.post('/api/knowledge/answer', json={'question': 'annual leave', 'generate_answer': True})
        assert response.status_code == 401 and 'Generated secret' not in response.text


def test_generation_disabled_fails_closed(mode, monkeypatch):  # noqa: F811
    import app.postgres_knowledge as knowledge

    monkeypatch.delenv('TAWZEEF_POSTGRES_ENABLE_GROQ', raising=False)
    monkeypatch.setattr(knowledge, 'generate_with_groq', lambda *_: pytest.fail('Unexpected model call'))
    with TestClient(create_app(seed_demo=True)) as candidate:
        login(candidate, 'candidate')
        response = candidate.post('/api/knowledge/answer', json={'question': 'annual leave', 'generate_answer': True})
        assert response.status_code == 503
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM knowledge_documents').fetchone()['n'] == 0


def test_title_only_fallback_does_not_send_to_provider(mode, monkeypatch):  # noqa: F811
    import app.postgres_knowledge as knowledge

    monkeypatch.setenv('TAWZEEF_POSTGRES_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'synthetic-test-only')
    monkeypatch.setattr(knowledge, 'generate_with_groq', lambda *_: pytest.fail('Title-only source sent to provider'))
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        login(hr, 'hr')
        login(candidate, 'candidate')
        assert hr.post('/api/knowledge/documents/text', json={'title': 'Synthetic leave policy',
                       'text': 'Leave requires approval.', 'audience': 'public', 'collection': 'handbook'}).status_code == 201
        response = candidate.post('/api/knowledge/answer', json={'question': 'What is the leave policy?', 'generate_answer': True})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['mode'] == 'extractive' and result['answer'] is None
        assert [source['title'] for source in result['sources']] == ['Synthetic leave policy']
        assert 'no question or excerpts were sent to Groq' in result['notice']

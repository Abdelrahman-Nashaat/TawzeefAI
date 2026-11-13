"""Synthetic role-filtered hybrid conversation retrieval and saved leads."""
import os

import pytest
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401

from app.main import create_app


def test_saved_passage_is_reconstructed_and_invalidated():
    from app.knowledge import _document_passages
    from app.postgres_conversations import _digest, _reconstruct

    row = {'id': 7, 'title': 'Fictional policy', 'body': 'alpha ' * 600,
           'page': 2, 'collection': 'handbook', 'audience': 'public', 'job_id': None, 'section': None}
    passages = _document_passages({'id': row['id'], 'title': row['title'], 'text': row['body']})
    assert len(passages) > 1
    ref = {'id': 7, 'digest': _digest(row), 'passage_id': passages[1].id}
    source = _reconstruct(ref, {7: row})
    assert source['id'] == passages[1].id and source['text'] == passages[1].text and source['page'] == 2
    assert _reconstruct(ref, {7: {**row, 'body': row['body'] + 'changed'}}) is None
    assert _reconstruct({**ref, 'passage_id': 'db-doc-7-p999'}, {7: row}) is None


def test_hybrid_conversation_reranks_and_revalidates_saved_leads(mode, monkeypatch):  # noqa: F811
    import app.postgres_knowledge as knowledge

    monkeypatch.setenv('TAWZEEF_POSTGRES_KNOWLEDGE_HYBRID', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'synthetic-model')
    monkeypatch.setenv('TAWZEEF_POSTGRES_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'synthetic-key')
    monkeypatch.setattr(knowledge, '_load_embedder', lambda _: object())
    seen, generated = [], []

    def semantic(_db, sources, _question, _model):
        seen.extend(item.title for item in sources)
        by_title = {item.title: item for item in sources}
        return [(0.99, by_title['Second']), (0.91, by_title['Lead'])]

    def generate(_question, sources, *, previous_question):
        generated.extend(source['title'] for source in sources)
        return f"Review the policy [{sources[0]['id']}]."

    monkeypatch.setattr(knowledge, '_semantic_candidates', semantic)
    monkeypatch.setattr(knowledge, 'generate_with_groq', generate)
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as other:
        login(hr, 'hr')
        login(candidate, 'candidate')
        login(other, 'candidate2')
        ids = {}
        for title, body, audience in [
            ('First', 'Annual leave annual leave requires approval.', 'public'),
            ('Second', 'Annual leave is ten days.', 'public'),
            ('Lead', 'The holiday period differs.', 'public'),
            ('Private', 'Annual leave confidential exception.', 'hr'),
        ]:
            response = hr.post('/api/knowledge/documents/text', json={'title': title, 'text': body,
                'audience': audience, 'collection': 'handbook', 'page': 7})
            assert response.status_code == 201
            ids[title] = response.json()['id']
        cid = candidate.post('/api/knowledge/conversations').json()['id']
        url = f'/api/knowledge/conversations/{cid}/turns'
        result = candidate.post(url, json={'question': 'annual leave', 'experimental_hybrid': True,
                                          'generate_answer': True})
        assert result.status_code == 201, result.text
        body = result.json()
        assert [item['title'] for item in body['sources']] == ['Second', 'First']
        assert [item['title'] for item in body['suggestions']] == ['Lead']
        assert generated == ['Second', 'First'] and 'Private' not in seen
        assert all(item['page'] == 7 for item in body['sources'] + body['suggestions'])
        assert other.get(url).status_code == 404
        saved = candidate.get(url).json()['items'][0]
        assert saved['mode'] == 'groq' and [item['title'] for item in saved['suggestions']] == ['Lead']
        assert hr.post(f"/api/knowledge/documents/{ids['Lead']}/deactivate").status_code == 200
        saved = candidate.get(url).json()['items'][0]
        assert saved['mode'] == 'groq' and saved['suggestions'] == [] and saved['suggestions_unavailable']
        assert hr.post(f"/api/knowledge/documents/{ids['Second']}/deactivate").status_code == 200
        saved = candidate.get(url).json()['items'][0]
        assert saved['mode'] == 'source_unavailable' and saved['answer'] is None and saved['sources'] == []


def test_hybrid_abstains_when_only_semantic_lead_matches(mode, monkeypatch):  # noqa: F811
    import app.postgres_knowledge as knowledge

    monkeypatch.setenv('TAWZEEF_POSTGRES_KNOWLEDGE_HYBRID', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'synthetic-model')
    monkeypatch.setenv('TAWZEEF_POSTGRES_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'synthetic-key')
    monkeypatch.setattr(knowledge, '_load_embedder', lambda _: object())
    monkeypatch.setattr(knowledge, '_semantic_candidates', lambda _db, sources, *_: [(0.99, sources[0])])
    monkeypatch.setattr(knowledge, 'generate_with_groq', lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('unexpected external call')))
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        login(hr, 'hr')
        login(candidate, 'candidate')
        assert hr.post('/api/knowledge/documents/text', json={'title': 'Holiday', 'text': 'The holiday period differs.',
            'audience': 'public', 'collection': 'handbook'}).status_code == 201
        cid = candidate.post('/api/knowledge/conversations').json()['id']
        result = candidate.post(f'/api/knowledge/conversations/{cid}/turns', json={'question': 'annual leave',
            'experimental_hybrid': True, 'generate_answer': True})
        assert result.status_code == 201, result.text
        assert result.json()['mode'] == 'abstain' and result.json()['answer'] is None
        assert [item['title'] for item in result.json()['suggestions']] == ['Holiday']


def test_hybrid_unavailable_does_not_save_turn(mode, monkeypatch):  # noqa: F811
    import app.postgres_knowledge as knowledge

    monkeypatch.setenv('TAWZEEF_POSTGRES_KNOWLEDGE_HYBRID', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'synthetic-model')
    monkeypatch.setattr(knowledge, '_load_embedder', lambda _: object())
    def fail(*_args):
        raise RuntimeError('private failure details')
    monkeypatch.setattr(knowledge, '_semantic_candidates', fail)
    with TestClient(create_app(seed_demo=True)) as candidate:
        login(candidate, 'candidate')
        cid = candidate.post('/api/knowledge/conversations').json()['id']
        url = f'/api/knowledge/conversations/{cid}/turns'
        response = candidate.post(url, json={'question': 'annual leave', 'experimental_hybrid': True})
        assert response.status_code == 503 and 'private failure details' not in response.text
        assert candidate.get(url).json()['items'] == []


@pytest.mark.parametrize('revoke_title', ['Holiday', 'Leave'])
def test_hybrid_source_revoked_during_generation_rolls_back(mode, monkeypatch, revoke_title):  # noqa: F811
    import app.postgres_knowledge as knowledge

    monkeypatch.setenv('TAWZEEF_POSTGRES_KNOWLEDGE_HYBRID', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'synthetic-model')
    monkeypatch.setenv('TAWZEEF_POSTGRES_ENABLE_GROQ', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'synthetic-key')
    monkeypatch.setattr(knowledge, '_load_embedder', lambda _: object())
    def semantic(_db, sources, *_):
        lead = next(source for source in sources if source.title == 'Holiday')
        return [(0.95, lead)]
    monkeypatch.setattr(knowledge, '_semantic_candidates', semantic)
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        login(hr, 'hr')
        login(candidate, 'candidate')
        for title, text in [('Leave', 'Annual leave is ten days.'), ('Holiday', 'The holiday period differs.')]:
            result = hr.post('/api/knowledge/documents/text', json={'title': title, 'text': text,
                'audience': 'public', 'collection': 'handbook'})
            assert result.status_code == 201
            if title == revoke_title:
                revoke_id = result.json()['id']
        cid = candidate.post('/api/knowledge/conversations').json()['id']
        def generate(_question, sources, *, previous_question):
            assert hr.post(f'/api/knowledge/documents/{revoke_id}/deactivate').status_code == 200
            return f"Ten days [{sources[0]['id']}]."
        monkeypatch.setattr(knowledge, 'generate_with_groq', generate)
        url = f'/api/knowledge/conversations/{cid}/turns'
        response = candidate.post(url, json={'question': 'annual leave', 'experimental_hybrid': True, 'generate_answer': True})
        assert response.status_code == 409 and candidate.get(url).json()['items'] == []


def test_conversation_uses_real_pgvector_with_authorized_passages(mode, monkeypatch):  # noqa: F811
    import app.postgres_knowledge as knowledge

    with db_for(mode) as db:
        if not db.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone():
            pytest.skip('A disposable PostgreSQL database with pgvector is required')
    class Model:
        def __init__(self):
            self.seen = []
        def encode(self, texts, *, query):
            if not query:
                self.seen.extend(texts)
            return [[1.0] + [0.0] * 383 for _ in texts]
    model = Model()
    monkeypatch.setenv('TAWZEEF_POSTGRES_KNOWLEDGE_HYBRID', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'synthetic-model')
    monkeypatch.setattr(knowledge, '_load_embedder', lambda _: model)
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        login(hr, 'hr')
        login(candidate, 'candidate')
        for title, body, audience in [('Public policy', 'The rest period is ten days.', 'public'),
                                      ('Secret policy', 'PRIVATE rest period information.', 'hr')]:
            assert hr.post('/api/knowledge/documents/text', json={'title': title, 'text': body,
                'audience': audience, 'collection': 'handbook', 'page': 7}).status_code == 201
        cid = candidate.post('/api/knowledge/conversations').json()['id']
        url = f'/api/knowledge/conversations/{cid}/turns'
        result = candidate.post(url, json={'question': 'vacation holiday', 'experimental_hybrid': True})
        assert result.status_code == 201, result.text
        assert result.json()['mode'] == 'abstain'
        assert [(s['title'], s['page']) for s in result.json()['suggestions']] == [('Public policy', 7)]
        assert not any('PRIVATE' in text for text in model.seen)
        saved = candidate.get(url).json()['items'][0]
        assert saved['suggestions'][0]['title'] == 'Public policy' and 'PRIVATE' not in repr(saved)
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM knowledge_vectors').fetchone()['n'] == 1


def test_local_model_suggests_saved_policy_lead(mode, monkeypatch):  # noqa: F811
    directory = os.environ.get('TAWZEEF_TEST_LOCAL_EMBEDDING_DIR')
    if not directory:
        pytest.skip('Set TAWZEEF_TEST_LOCAL_EMBEDDING_DIR to the pinned local model')
    with db_for(mode) as db:
        if not db.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone():
            pytest.skip('A disposable PostgreSQL database with pgvector is required')
    monkeypatch.setenv('TAWZEEF_POSTGRES_KNOWLEDGE_HYBRID', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', directory)
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        login(hr, 'hr')
        login(candidate, 'candidate')
        assert hr.post('/api/knowledge/documents/text', json={'title': 'Synthetic policy',
            'text': 'Employees must request annual leave from their manager.', 'audience': 'public',
            'collection': 'handbook', 'page': 4}).status_code == 201
        cid = candidate.post('/api/knowledge/conversations').json()['id']
        url = f'/api/knowledge/conversations/{cid}/turns'
        answer = candidate.post(url, json={'question': 'How do staff request vacation?', 'experimental_hybrid': True})
        assert answer.status_code == 201, answer.text
        assert answer.json()['mode'] == 'abstain'
        assert [(s['title'], s['page']) for s in answer.json()['suggestions']] == [('Synthetic policy', 4)]
        assert [s['title'] for s in candidate.get(url).json()['items'][0]['suggestions']] == ['Synthetic policy']

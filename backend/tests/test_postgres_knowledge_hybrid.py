"""Role-filtered hybrid knowledge search with an operator-provisioned pgvector."""
import os
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401

from app.main import create_app


class Model:
    def __init__(self):
        self.seen = []

    def encode(self, texts, *, query):
        if not query:
            self.seen.extend(texts)
        return [[1.0, 0.0] + [0.0] * 382 if query or 'rest period' in text
                else [0.0, 1.0] + [0.0] * 382 for text in texts]


def test_hybrid_fuses_authorized_lexical_order_and_keeps_leads_separate(mode, monkeypatch):  # noqa: F811
    import app.postgres_knowledge as knowledge

    monkeypatch.setenv('TAWZEEF_POSTGRES_KNOWLEDGE_HYBRID', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'synthetic-test-model')
    monkeypatch.setattr(knowledge, '_load_embedder', lambda _: object())
    seen = []

    def semantic(_db, sources, _question, _model):
        seen.extend(source.title for source in sources)
        by_title = {source.title: source for source in sources}
        return [(0.99, by_title['Second']), (0.91, by_title['Lead'])]

    monkeypatch.setattr(knowledge, '_semantic_candidates', semantic)
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        login(hr, 'hr')
        login(candidate, 'candidate')
        for title, body, audience in [
            ('First', 'Annual leave annual leave requires approval.', 'public'),
            ('Second', 'Annual leave is ten days.', 'public'),
            ('Lead', 'The holiday period differs.', 'public'),
            ('Private', 'Annual leave confidential exception.', 'hr'),
        ]:
            assert hr.post('/api/knowledge/documents/text', json={'title': title, 'text': body,
                'audience': audience, 'collection': 'handbook', 'page': 7}).status_code == 201
        response = candidate.post('/api/knowledge/answer', json={'question': 'annual leave',
                                  'experimental_hybrid': True})
        assert response.status_code == 200, response.text
        result = response.json()
        assert [item['title'] for item in result['sources']] == ['Second', 'First']
        assert [item['title'] for item in result['suggestions']] == ['Lead']
        assert all(item['page'] == 7 for item in result['sources'] + result['suggestions'])
        assert 'Private' not in seen and 'Private' not in response.text


def test_pgvector_hybrid_access_revocation_and_expiry(mode, monkeypatch):  # noqa: F811
    import app.postgres_experimental as pg_app
    import app.postgres_knowledge as knowledge

    with db_for(mode) as db:
        if not db.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone():
            pytest.skip('A disposable PostgreSQL database with pgvector is required')
    monkeypatch.setenv('TAWZEEF_POSTGRES_KNOWLEDGE_HYBRID', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'synthetic-test-model')
    model = Model()
    monkeypatch.setattr(knowledge, '_load_embedder', lambda _: model)
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        login(hr, 'hr')
        login(candidate, 'candidate')
        def add(title, body, audience):
            response = hr.post('/api/knowledge/documents/text', json={'title': title, 'text': body,
                'audience': audience, 'collection': 'handbook', 'page': 7})
            assert response.status_code == 201, response.text
            return response.json()['id']
        public = add('Public policy', 'The rest period is ten days.', 'public')
        add('Secret policy', 'PRIVATE rest period information.', 'hr')
        response = candidate.post('/api/knowledge/answer', json={'question': 'vacation holiday', 'experimental_hybrid': True})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['mode'] == 'abstain' and data['sources'] == []
        assert [(s['title'], s['page']) for s in data['suggestions']] == [('Public policy', 7)]
        assert not any('PRIVATE' in text for text in model.seen)
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM knowledge_vectors').fetchone()['n'] == 1
        # An HR search persists a private vector in the shared index. A later
        # candidate search must still restrict the query to authorized IDs.
        assert hr.post('/api/knowledge/answer', json={'question': 'vacation holiday',
                       'experimental_hybrid': True}).status_code == 200
        again = candidate.post('/api/knowledge/answer', json={'question': 'vacation holiday',
                               'experimental_hybrid': True})
        assert again.status_code == 200 and 'PRIVATE' not in again.text and 'Secret policy' not in again.text
        assert hr.post(f'/api/knowledge/documents/{public}/deactivate').status_code == 200
        response = candidate.post('/api/knowledge/answer', json={'question': 'vacation holiday', 'experimental_hybrid': True})
        assert response.json()['suggestions'] == []
        public = add('Second public', 'The rest period requires approval.', 'public')
        original = knowledge._semantic_candidates
        def expire(*args):
            result = original(*args)
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            return result
        monkeypatch.setattr(knowledge, '_semantic_candidates', expire)
        expired = candidate.post('/api/knowledge/answer', json={'question': 'vacation holiday', 'experimental_hybrid': True})
        assert expired.status_code == 401 and 'rest period' not in expired.text


def test_hybrid_is_explicitly_opt_in(mode, monkeypatch):  # noqa: F811
    monkeypatch.delenv('TAWZEEF_POSTGRES_KNOWLEDGE_HYBRID', raising=False)
    with TestClient(create_app(seed_demo=True)) as candidate:
        login(candidate, 'candidate')
        response = candidate.post('/api/knowledge/answer', json={'question': 'vacation holiday', 'experimental_hybrid': True})
        assert response.status_code == 503
        assert candidate.post('/api/knowledge/answer', json={'question': 'vacation holiday'}).status_code == 200


def test_live_local_model_suggests_related_authorized_passage(mode, monkeypatch):  # noqa: F811
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
        response = hr.post('/api/knowledge/documents/text', json={'title': 'Synthetic policy',
            'text': 'Employees must request annual leave from their manager.', 'audience': 'public',
            'collection': 'handbook', 'page': 4})
        assert response.status_code == 201
        answer = candidate.post('/api/knowledge/answer', json={'question': 'How do staff request vacation?',
                                'experimental_hybrid': True})
        assert answer.status_code == 200, answer.text
        assert answer.json()['mode'] == 'abstain'
        assert [source['title'] for source in answer.json()['suggestions']] == ['Synthetic policy']

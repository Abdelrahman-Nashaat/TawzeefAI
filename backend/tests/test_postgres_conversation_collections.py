"""Collection selection must constrain retrieval before model or embedding calls."""
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401

from app.main import create_app


def test_collection_filter_persists_and_never_exposes_hr_patterns(mode, monkeypatch):  # noqa: F811
    import app.postgres_knowledge as knowledge

    seen = []
    monkeypatch.setenv('TAWZEEF_POSTGRES_KNOWLEDGE_HYBRID', '1')
    monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'synthetic')
    monkeypatch.setattr(knowledge, '_load_embedder', lambda _: object())
    def semantic(_db, sources, _question, _model):
        seen.append([source.title for source in sources])
        return []
    monkeypatch.setattr(knowledge, '_semantic_candidates', semantic)
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        login(hr, 'hr')
        login(candidate, 'candidate')
        for title, collection, audience in [('Handbook', 'handbook', 'public'),
                                             ('Job', 'job_description', 'public'),
                                             ('Private pattern', 'past_hire_patterns', 'hr')]:
            response = hr.post('/api/knowledge/documents/text', json={'title': title, 'text': 'Python analytics role.',
                'audience': audience, 'collection': collection, 'job_id': 'analyst', 'page': 1})
            assert response.status_code == 201, response.text
        cid = candidate.post('/api/knowledge/conversations').json()['id']
        path = f'/api/knowledge/conversations/{cid}/turns'
        response = candidate.post(path, json={'question': 'python analytics', 'job_id': 'analyst',
                                              'collection': 'job_description', 'experimental_hybrid': True})
        assert response.status_code == 201, response.text
        assert [source['title'] for source in response.json()['sources']] == ['Job']
        assert response.json()['sources'][0]['collection'] == 'job_description'
        assert seen == [['Job']]
        saved = candidate.get(path).json()['items'][0]
        assert saved['collection_filter'] == 'job_description'
        assert [source['title'] for source in saved['sources']] == ['Job']
        forbidden = candidate.post(path, json={'question': 'python analytics', 'collection': 'past_hire_patterns'})
        assert forbidden.status_code == 201 and forbidden.json()['mode'] == 'abstain'
        assert 'Private pattern' not in forbidden.text
        assert candidate.post(path, json={'question': 'python analytics', 'collection': 'invalid'}).status_code == 422
        with db_for(mode) as db:
            db.execute("UPDATE knowledge_documents SET collection='handbook' WHERE title='Job'")
        assert candidate.get(path).json()['items'][0]['mode'] == 'source_unavailable'


def test_existing_unfiltered_turn_survives_upgrade(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        login(hr, 'hr')
        login(candidate, 'candidate')
        assert hr.post('/api/knowledge/documents/text', json={'title': 'Policy', 'text': 'Annual leave policy.',
                       'audience': 'public', 'collection': 'handbook'}).status_code == 201
        cid = candidate.post('/api/knowledge/conversations').json()['id']
        path = f'/api/knowledge/conversations/{cid}/turns'
        assert candidate.post(path, json={'question': 'annual leave'}).status_code == 201
        with db_for(mode) as db:
            db.execute('ALTER TABLE knowledge_conversation_turns DROP COLUMN collection_filter')
    with TestClient(create_app(seed_demo=True)) as candidate:
        login(candidate, 'candidate')
        item = candidate.get(path).json()['items'][0]
        assert item['collection_filter'] is None and item['mode'] == 'extractive'

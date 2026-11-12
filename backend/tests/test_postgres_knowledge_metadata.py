"""Role-scoped knowledge metadata and source revision integration."""
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401

from app.main import create_app


def test_metadata_filters_and_saved_source_revalidation(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        login(hr, 'hr')
        login(candidate, 'candidate')
        def create(title, job, section, audience='public'):
            response = hr.post('/api/knowledge/documents/text', json={
                'title': title, 'text': 'Python analytics model training responsibilities.',
                'audience': audience, 'collection': 'job_description',
                'page': 2, 'job_id': job, 'section': section})
            assert response.status_code == 201, response.text
            return response.json()['id']
        analyst = create('Analyst', 'data-analyst', 'Tools')
        create('Scientist', 'data-scientist', 'Tools')
        create('Private analyst', 'data-analyst', 'Tools', 'hr')
        response = candidate.post('/api/knowledge/answer', json={'question': 'python analytics',
            'collection': 'job_description', 'job_id': 'data-analyst', 'section': 'Tools'})
        assert response.status_code == 200, response.text
        sources = response.json()['sources']
        assert len(sources) == 1 and sources[0]['title'] == 'Analyst'
        assert sources[0]['page'] == 2 and sources[0]['job_id'] == 'data-analyst' and sources[0]['section'] == 'Tools'
        assert candidate.post('/api/knowledge/answer', json={'question': 'python analytics',
            'job_id': 'data-scientist', 'section': 'Other'}).json()['mode'] == 'abstain'
        cid = candidate.post('/api/knowledge/conversations').json()['id']
        turn = candidate.post(f'/api/knowledge/conversations/{cid}/turns', json={
            'question': 'python analytics', 'job_id': 'data-analyst', 'section': 'Tools'})
        assert turn.status_code == 201 and [s['title'] for s in turn.json()['sources']] == ['Analyst']
        with db_for(mode) as db:
            db.execute('UPDATE knowledge_documents SET section=%s WHERE id=%s', ('Changed', analyst))
        saved = candidate.get(f'/api/knowledge/conversations/{cid}/turns').json()['items'][0]
        assert saved['mode'] == 'source_unavailable' and saved['sources'] == []


def test_invalid_metadata_is_rejected(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        doc = {'title': 'Example', 'text': 'Python analytics', 'audience': 'public', 'collection': 'handbook'}
        assert hr.post('/api/knowledge/documents/text', json={**doc, 'job_id': '../escape'}).status_code == 422
        assert hr.post('/api/knowledge/documents/text', json={**doc, 'section': '  '}).status_code == 422
        assert hr.post('/api/knowledge/answer', json={'question': 'python analytics', 'job_id': 'A B'}).status_code == 422


def test_upgrade_preserves_pre_metadata_conversations(mode):  # noqa: F811
    import hashlib
    import json

    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        login(hr, 'hr')
        login(candidate, 'candidate')
        doc = hr.post('/api/knowledge/documents/text', json={
            'title': 'Original', 'text': 'Annual leave requires approval.',
            'audience': 'public', 'collection': 'handbook'}).json()
        cid = candidate.post('/api/knowledge/conversations').json()['id']
        assert candidate.post(f'/api/knowledge/conversations/{cid}/turns', json={
            'question': 'annual leave'}).status_code == 201
        with db_for(mode) as db:
            row = db.execute('SELECT id,title,body,page,collection,audience FROM knowledge_documents WHERE id=%s',
                             (doc['id'],)).fetchone()
            value = [row[k] for k in ('id', 'title', 'body', 'page', 'collection', 'audience')]
            legacy_digest = hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            stored = json.loads(db.execute('SELECT sources_json FROM knowledge_conversation_turns').fetchone()['sources_json'])
            assert stored == [{'id': doc['id'], 'digest': legacy_digest}]
            db.execute('ALTER TABLE knowledge_documents DROP COLUMN job_id, DROP COLUMN section')
    with TestClient(create_app(seed_demo=True)) as candidate:
        login(candidate, 'candidate')
        saved = candidate.get(f'/api/knowledge/conversations/{cid}/turns').json()['items'][0]
        assert saved['mode'] == 'extractive'
        assert [s['title'] for s in saved['sources']] == ['Original']
        assert 'job_id' not in saved['sources'][0] and 'section' not in saved['sources'][0]

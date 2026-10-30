from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def test_hybrid_answer_uses_only_current_authorized_passages(tmp_path, monkeypatch):
    from app import hybrid_api, knowledge

    path = tmp_path / 'hybrid-answer.sqlite3'
    monkeypatch.setenv('TAWZEEF_USE_HYBRID_ANSWERS', '1')
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        public = client.post('/api/knowledge/documents/text', json={
            'title': 'Hybrid example', 'audience': 'public', 'text': 'Vacation entitlement is twelve days.',
        }).json()['id']
        private = client.post('/api/knowledge/documents/text', json={
            'title': 'Secret', 'audience': 'hr', 'text': 'PRIVATE HR DOCUMENT',
        }).json()['id']
        assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200

        def retrieve(path, role, question):
            assert role == 'candidate'
            return {'status': 'ok', 'sources': [
                {'id': f'db-doc-{private}-p1', 'title': 'Secret', 'text': 'PRIVATE HR DOCUMENT', 'page': None},
                {'id': f'db-doc-{public}-p1', 'title': 'Hybrid example', 'text': 'Vacation entitlement is twelve days.', 'page': None},
            ]}

        seen = []
        monkeypatch.setattr(hybrid_api, 'retrieve_hybrid', retrieve)
        monkeypatch.setattr(knowledge, 'generate_with_groq', lambda question, sources: seen.extend(sources) or f"Twelve days [db-doc-{public}-p1]")
        response = client.post('/api/knowledge/answer', json={'question': 'How many holidays are granted?'})
        assert response.status_code == 200
        assert response.json()['sources'][0]['id'] == f'db-doc-{public}-p1'
        assert 'PRIVATE HR DOCUMENT' not in str(seen) and 'PRIVATE HR DOCUMENT' not in response.text


def test_hybrid_failure_falls_back_to_lexical_with_notice(tmp_path, monkeypatch):
    from app import hybrid_api

    monkeypatch.setenv('TAWZEEF_USE_HYBRID_ANSWERS', '1')
    monkeypatch.setattr(hybrid_api, 'retrieve_hybrid', lambda *args: (_ for _ in ()).throw(RuntimeError('secret')))
    app = create_app(tmp_path / 'fallback.sqlite3', seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'employee', 'password': 'employee-demo-only'}).status_code == 200
        result = client.post('/api/knowledge/answer', json={'question': 'What is the annual leave policy?'})
        assert result.status_code == 200
        assert result.json()['sources']
        assert 'hybrid' in result.json()['notice'].lower()
        assert 'secret' not in result.text


def test_hybrid_source_revoked_before_generation_is_not_sent(tmp_path, monkeypatch):
    from app import hybrid_api, knowledge

    path = tmp_path / 'revoked.sqlite3'
    monkeypatch.setenv('TAWZEEF_USE_HYBRID_ANSWERS', '1')
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        doc = client.post('/api/knowledge/documents/text', json={
            'title': 'Hidden', 'audience': 'public', 'text': 'Vacation entitlement is twelve days.',
        }).json()['id']
        def retrieve(path, role, question):
            with connect(path) as db:
                db.execute('UPDATE knowledge_documents SET active=0 WHERE id=?', (doc,))
            return {'status': 'ok', 'sources': [{'id': f'db-doc-{doc}-p1', 'title': 'Hidden',
                'text': 'Vacation entitlement is twelve days.', 'page': None}]}
        seen = []
        monkeypatch.setattr(hybrid_api, 'retrieve_hybrid', retrieve)
        monkeypatch.setattr(knowledge, 'generate_with_groq', lambda question, sources: seen.extend(sources) or 'answer')
        response = client.post('/api/knowledge/answer', json={'question': 'How many holidays are granted?'})
        assert response.status_code in (200, 409)
        assert 'Vacation entitlement' not in str(seen) and 'Vacation entitlement' not in response.text


def test_revoked_session_during_hybrid_search_never_reaches_provider(tmp_path, monkeypatch):
    from app import hybrid_api, knowledge

    path = tmp_path / 'session.sqlite3'
    monkeypatch.setenv('TAWZEEF_USE_HYBRID_ANSWERS', '1')
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        doc = client.post('/api/knowledge/documents/text', json={
            'title': 'Synthetic', 'audience': 'public', 'text': 'Vacation entitlement is twelve days.',
        }).json()['id']
        def retrieve(path, role, question):
            with connect(path) as db:
                db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='hr')")
            return {'status': 'ok', 'sources': [{'id': f'db-doc-{doc}-p1', 'title': 'Synthetic',
                'text': 'Vacation entitlement is twelve days.', 'page': None}]}
        called = []
        monkeypatch.setattr(hybrid_api, 'retrieve_hybrid', retrieve)
        monkeypatch.setattr(knowledge, 'generate_with_groq', lambda *args: called.append(args) or 'answer')
        result = client.post('/api/knowledge/answer', json={'question': 'How many holidays are granted?'})
        assert result.status_code == 401
        assert called == []


def test_hybrid_chat_persists_authorized_result(tmp_path, monkeypatch):
    from app import hybrid_api

    path = tmp_path / 'hybrid-chat.sqlite3'
    monkeypatch.setenv('TAWZEEF_USE_HYBRID_ANSWERS', '1')
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        doc = client.post('/api/knowledge/documents/text', json={
            'title': 'Synthetic', 'audience': 'public', 'text': 'Vacation entitlement is twelve days.',
        }).json()['id']
        conversation_id = client.post('/api/conversations', json={'title': 'Test'}).json()['id']
        monkeypatch.setattr(hybrid_api, 'retrieve_hybrid', lambda *args: {'status': 'ok', 'sources': [{
            'id': f'db-doc-{doc}-p1', 'title': 'Synthetic',
            'text': 'Vacation entitlement is twelve days.', 'page': None,
        }]})
        response = client.post(f'/api/conversations/{conversation_id}/messages',
                               json={'question': 'How many holidays are granted?'})
        assert response.status_code == 201
        assert response.json()['sources'][0]['id'] == f'db-doc-{doc}-p1'
        assert 'hybrid' in response.json()['notice'].lower()
        stored = client.get(f'/api/conversations/{conversation_id}').json()['messages']
        assert stored[0]['sources'][0]['id'] == f'db-doc-{doc}-p1'


def test_chat_role_change_during_hybrid_search_never_reaches_provider(tmp_path, monkeypatch):
    from app import hybrid_api, knowledge

    path = tmp_path / 'hybrid-chat-role.sqlite3'
    monkeypatch.setenv('TAWZEEF_USE_HYBRID_ANSWERS', '1')
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        doc = client.post('/api/knowledge/documents/text', json={
            'title': 'Synthetic', 'audience': 'public', 'text': 'Vacation entitlement is twelve days.',
        }).json()['id']
        conversation_id = client.post('/api/conversations', json={'title': 'Test'}).json()['id']

        def retrieve(path, role, question):
            with connect(path) as db:
                db.execute("UPDATE users SET role='candidate' WHERE username='hr'")
            return {'status': 'ok', 'sources': [{'id': f'db-doc-{doc}-p1', 'title': 'Synthetic',
                'text': 'Vacation entitlement is twelve days.', 'page': None}]}

        called = []
        monkeypatch.setattr(hybrid_api, 'retrieve_hybrid', retrieve)
        monkeypatch.setattr(knowledge, 'generate_with_groq', lambda *args: called.append(args) or 'answer')
        response = client.post(f'/api/conversations/{conversation_id}/messages',
                               json={'question': 'How many holidays are granted?'})
        assert response.status_code == 403
        assert called == []

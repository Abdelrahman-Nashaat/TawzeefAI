import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def test_knowledge_endpoint_requires_auth_and_derives_role_from_session(tmp_path):
    app = create_app(database_path=tmp_path / "test.sqlite3", seed_demo=True)
    with TestClient(app) as client:
        question = 'What is the recruiter review procedure?'
        assert client.post('/api/knowledge/answer', json={'question': question}).status_code == 401
        login = client.post('/api/auth/login', json={
            'username': 'candidate', 'password': 'candidate-demo-only',
        })
        assert login.status_code == 200
        response = client.post('/api/knowledge/answer', json={
            'question': question, 'role': 'hr',
        })
        assert response.status_code == 200
        assert all(source['id'] != 'pol-hr-review' for source in response.json()['sources'])
        client.post('/api/auth/logout')
        assert client.post('/api/auth/login', json={
            'username': 'hr', 'password': 'hr-demo-only',
        }).status_code == 200
        hr_response = client.post('/api/knowledge/answer', json={'question': question})
        assert any(source['id'] == 'pol-hr-review' for source in hr_response.json()['sources'])


def test_frontend_is_served_without_shadowing_api(tmp_path):
    with TestClient(create_app(database_path=tmp_path / 'test.sqlite3')) as client:
        assert client.get('/').status_code == 200
        assert client.get('/assets/app.js').status_code == 200
        assert client.get('/api/health').json()['status'] == 'ok'
        assert client.get('/api/health', headers={'host': 'attacker.example'}).status_code == 400


@pytest.mark.parametrize('change', ['deactivate', 'modify', 'role', 'session'])
def test_knowledge_revalidates_sources_and_session_after_provider_call(tmp_path, monkeypatch, change):
    from app import knowledge

    path = tmp_path / 'recheck.sqlite3'
    monkeypatch.setenv('TAWZEEF_ENABLE_GROQ', '1')
    app = create_app(database_path=path, seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        created = client.post('/api/knowledge/documents/text', json={
            'title': 'Synthetic knowledge', 'audience': 'staff',
            'text': 'Nebula leave policy permits sixteen fictional days.',
        })
        assert created.status_code == 201
        document_id = created.json()['id']
        assert client.post('/api/auth/login', json={'username': 'employee', 'password': 'employee-demo-only'}).status_code == 200

        def provider(question, sources):
            assert len(sources) == 1 and 'Nebula leave' in sources[0]['text']
            with connect(path) as db:
                if change == 'deactivate':
                    db.execute('UPDATE knowledge_documents SET active=0 WHERE id=?', (document_id,))
                elif change == 'modify':
                    db.execute("UPDATE knowledge_documents SET text='Different document' WHERE id=?", (document_id,))
                elif change == 'role':
                    db.execute("UPDATE users SET role='candidate' WHERE username='employee'")
                else:
                    db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='employee')")
            return f"Private stale claim [{sources[0]['id']}]"

        monkeypatch.setattr(knowledge, 'generate_with_groq', provider)
        response = client.post('/api/knowledge/answer', json={'question': 'Nebula leave policy'})
        assert response.status_code in (401, 409)
        assert 'Private stale claim' not in response.text
        assert 'sixteen fictional' not in response.text


def test_extractive_endpoint_revalidates_source_changes(tmp_path, monkeypatch):
    from app import main

    path = tmp_path / 'extractive-recheck.sqlite3'
    app = create_app(database_path=path, seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        doc = client.post('/api/knowledge/documents/text', json={
            'title': 'Synthetic', 'audience': 'public', 'text': 'Orion leave policy allows thirteen days.',
        }).json()['id']
        original = main.answer_question
        def altered(*args, **kwargs):
            answer = original(*args, **kwargs)
            with connect(path) as db:
                db.execute('UPDATE knowledge_documents SET active=0 WHERE id=?', (doc,))
            return answer
        monkeypatch.setattr(main, 'answer_question', altered)
        response = client.post('/api/knowledge/answer', json={'question': 'Orion leave policy'})
        assert response.status_code == 409
        assert 'thirteen days' not in response.text

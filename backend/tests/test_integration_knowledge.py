from fastapi.testclient import TestClient

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

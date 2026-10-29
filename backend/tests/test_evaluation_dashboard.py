from fastapi.testclient import TestClient

from app.main import create_app


def test_synthetic_evaluation_is_hr_only_and_explicitly_limited(tmp_path):
    with TestClient(create_app(tmp_path / 'eval.sqlite3', seed_demo=True)) as client:
        url = '/api/evaluation/capstone'
        assert client.get(url).status_code == 401
        for role in ('candidate', 'employee'):
            assert client.post('/api/auth/login', json={
                'username': role, 'password': f'{role}-demo-only',
            }).status_code == 200
            assert client.get(url).status_code == 403
        assert client.post('/api/auth/login', json={
            'username': 'hr', 'password': 'hr-demo-only',
        }).status_code == 200
        response = client.get(url)
        assert response.status_code == 200
        result = response.json()
        assert result['summary']['retrieval_total'] == 30
        assert result['summary']['ranking_total'] == 10
        assert result['settings']['answer_correctness'] == 'not evaluated'
        assert 'Synthetic' in result['disclaimer']
        assert len(result['missed_retrieval_cases']) == (
            30 - result['summary']['retrieval_recall_at_3']
        )


def test_hybrid_availability_is_hidden_until_explicitly_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv('TAWZEEF_ENABLE_HYBRID', raising=False)
    monkeypatch.delenv('TAWZEEF_EMBEDDING_MODEL_DIR', raising=False)
    with TestClient(create_app(tmp_path / 'hybrid-status.sqlite3', seed_demo=True)) as client:
        assert client.get('/api/knowledge/hybrid-status').status_code == 401
        assert client.post('/api/auth/login', json={
            'username': 'candidate', 'password': 'candidate-demo-only',
        }).status_code == 200
        assert client.get('/api/knowledge/hybrid-status').json() == {'configured': False}
        assert client.post('/api/knowledge/experimental-hybrid', json={'question': 'leave'}).status_code == 503
        monkeypatch.setenv('TAWZEEF_ENABLE_HYBRID', '1')
        monkeypatch.setenv('TAWZEEF_EMBEDDING_MODEL_DIR', 'local-model')
        assert client.get('/api/knowledge/hybrid-status').json() == {'configured': True}

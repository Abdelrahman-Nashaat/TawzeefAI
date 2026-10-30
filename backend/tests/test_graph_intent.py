import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.mark.parametrize(('intent', 'expected', 'present', 'absent'), [
    ('status', ['status', 'human_review'], 'status', ('parsed', 'matching', 'scheduling', 'policy', 'agent_review')),
    ('scheduling', ['scheduler', 'human_review'], 'scheduling', ('parsed', 'matching', 'status', 'policy', 'agent_review')),
    ('policy', ['policy', 'human_review'], 'policy', ('parsed', 'matching', 'status', 'scheduling', 'agent_review')),
])
def test_selected_intent_routes_and_survives_restart(tmp_path, monkeypatch, intent, expected, present, absent):
    monkeypatch.delenv('TAWZEEF_ENABLE_GROQ', raising=False)
    path = tmp_path / 'intent.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?, 'job-backend-python', 'r.docx', 'synthetic')", (candidate,)).lastrowid
        payload = {'application_id': app_id, 'intent': intent}
        if intent == 'policy':
            payload['policy_question'] = 'annual leave policy'
        response = client.post('/api/graph-reviews', json=payload)
        assert response.status_code == 201, response.text
        review = response.json()
        assert review['awaiting_review'] and review['state']['plan'] == expected
        assert review['state']['intent'] == intent and present in review['state']
        assert all(key not in review['state'] for key in absent)
        saved = client.get('/api/graph-reviews/' + review['id']).json()
        assert saved['state']['plan'] == expected
        assert all(key not in saved['state'] for key in absent)
        resumed = client.post('/api/graph-reviews/' + review['id'] + '/resume', json={'reviewed': True})
        assert resumed.status_code == 200 and not resumed.json()['awaiting_review']
        assert resumed.json()['state']['plan'] == expected


def test_intent_validation_rejects_missing_or_unexpected_question(tmp_path):
    path = tmp_path / 'invalid.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        for payload in ({'intent': 'policy'}, {'intent': 'status', 'policy_question': 'question'}, {'intent': 'other'}):
            assert client.post('/api/graph-reviews', json={'application_id': 1, **payload}).status_code == 422
        with connect(path) as db:
            table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='graph_review_owners'").fetchone()
            assert table is None or db.execute('SELECT count(*) FROM graph_review_owners').fetchone()[0] == 0

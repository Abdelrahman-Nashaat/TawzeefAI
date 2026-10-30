from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def test_revoked_session_does_not_resume_review(tmp_path, monkeypatch):
    from app import graph_orchestrator

    path = tmp_path / 'review.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES(?, 'job-backend-python', 'synthetic.docx', 'synthetic')", (candidate,)).lastrowid
        created = client.post('/api/graph-reviews', json={'application_id': app_id})
        assert created.status_code == 201
        thread_id = created.json()['id']
        original = graph_orchestrator._owner

        def revoke_after_ownership(path, thread_id, user):
            original(path, thread_id, user)
            with connect(path) as db:
                db.execute('DELETE FROM sessions WHERE user_id=?', (user.id,))

        with monkeypatch.context() as m:
            m.setattr(graph_orchestrator, '_owner', revoke_after_ownership)
            response = client.post(f'/api/graph-reviews/{thread_id}/resume', json={'reviewed': True})
            assert response.status_code == 401
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        restored = client.get(f'/api/graph-reviews/{thread_id}')
        assert restored.status_code == 200
        assert restored.json()['awaiting_review'] is True
        assert 'reviewed' not in restored.json()['state']

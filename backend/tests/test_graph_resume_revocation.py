from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def test_revocation_serializes_after_resume_transaction(tmp_path, monkeypatch):
    import threading

    from app import graph_orchestrator

    path = tmp_path / 'race.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            app_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES(?, 'job-backend-python', 'x.docx', 'synthetic')", (candidate,)).lastrowid
        review = client.post('/api/graph-reviews', json={'application_id': app_id}).json()['id']
        started, finished = threading.Event(), threading.Event()
        original = graph_orchestrator._authorize_resume
        worker = None

        def revoke():
            started.set()
            with connect(path) as db:
                db.execute('DELETE FROM sessions WHERE user_id=?', (hr,))
            finished.set()

        def authorize(db, request, user, thread_id):
            nonlocal worker
            original(db, request, user, thread_id)
            if worker is None:
                worker = threading.Thread(target=revoke)
                worker.start()
                assert started.wait(5)
                # The competing writer cannot complete before checkpoint commit.
                assert not finished.wait(0.2)

        with monkeypatch.context() as patch:
            patch.setattr(graph_orchestrator, '_authorize_resume', authorize)
            response = client.post(f'/api/graph-reviews/{review}/resume', json={'reviewed': True})
        assert response.status_code in (200, 401)
        assert worker is not None
        worker.join(timeout=10)
        assert finished.is_set()
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        state = client.get(f'/api/graph-reviews/{review}').json()
        assert not state['awaiting_review'] and state['state']['reviewed'] is True


def test_session_expiry_before_resume_commit_rolls_back(tmp_path, monkeypatch):
    import time
    from types import SimpleNamespace

    from app import graph_orchestrator

    path = tmp_path / 'expiry.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES(?, 'job-backend-python', 'x.docx', 'synthetic')", (candidate,)).lastrowid
        review = client.post('/api/graph-reviews', json={'application_id': app_id}).json()['id']
        times = iter([time.time(), time.time() + 60 * 60 * 24 * 365])
        with monkeypatch.context() as patch:
            patch.setattr(graph_orchestrator, 'time', SimpleNamespace(time=lambda: next(times)))
            assert client.post(f'/api/graph-reviews/{review}/resume', json={'reviewed': True}).status_code == 401
        restored = client.get(f'/api/graph-reviews/{review}').json()
        assert restored['awaiting_review'] is True and 'reviewed' not in restored['state']
        result = client.post(f'/api/graph-reviews/{review}/resume', json={'reviewed': False}).json()
        assert result['state']['reviewed'] is False


def test_failed_resume_does_not_persist_partial_checkpoint_writes(tmp_path, monkeypatch):
    from langgraph.checkpoint.sqlite import SqliteSaver

    path = tmp_path / 'rollback.sqlite3'
    with TestClient(create_app(path, seed_demo=True), raise_server_exceptions=False) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES(?, 'job-backend-python', 'x.docx', 'synthetic')", (candidate,)).lastrowid
        review = client.post('/api/graph-reviews', json={'application_id': app_id}).json()['id']
        original = SqliteSaver.put_writes

        def fail_after_writes(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise RuntimeError('synthetic crash after pending writes')

        with monkeypatch.context() as patch:
            patch.setattr(SqliteSaver, 'put_writes', fail_after_writes)
            assert client.post(f'/api/graph-reviews/{review}/resume', json={'reviewed': True}).status_code == 500
        restored = client.get(f'/api/graph-reviews/{review}').json()
        assert restored['awaiting_review'] is True
        assert 'reviewed' not in restored['state']
        # The next authorized decision must not inherit a pending prior answer.
        resumed = client.post(f'/api/graph-reviews/{review}/resume', json={'reviewed': False})
        assert resumed.status_code == 200
        assert resumed.json()['state']['reviewed'] is False


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

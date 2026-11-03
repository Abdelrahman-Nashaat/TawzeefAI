"""The HR status specialist reports the current candidate-visible lifecycle stage."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def login(client, role):
    assert client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'}).status_code == 200


def test_graph_status_tracks_real_lifecycle_without_inferencing_completion(tmp_path):
    path = tmp_path / 'status.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            app = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?, 'job-backend-python', 'fictional.pdf', 'Python')", (candidate,)).lastrowid
        created = client.post('/api/graph-reviews', json={'application_id': app, 'intent': 'status'})
        assert created.status_code == 201, created.text
        review_id = created.json()['id']
        endpoint = f'/api/graph-reviews/{review_id}'
        def stage():
            result = client.get(endpoint)
            assert result.status_code == 200, result.text
            progress = result.json()['state']['candidate_progress']
            assert set(progress) == {'stage', 'notice'}
            assert 'Live' in progress['notice']
            return progress['stage']
        assert stage() == 'applied'
        with connect(path) as db:
            for row in db.execute('SELECT checkpoint FROM checkpoints'):
                assert b'candidate_progress' not in bytes(row[0])
        assert client.post(f'/api/applications/{app}/screen').status_code == 200
        assert stage() == 'screened'
        assert client.post(f'/api/applications/{app}/decision', json={'decision': 'advance'}).status_code == 200
        assert stage() == 'shortlisted'
        with connect(path) as db:
            interview = db.execute("""INSERT INTO interviews(application_id,candidate_id,start_at,end_at,interviewer,interviewer_key,status,created_by)
                VALUES (?,?,'2020-01-01T10:00:00.000000Z','2020-01-01T11:00:00.000000Z','Synthetic','synthetic','scheduled',?)""", (app, candidate, hr)).lastrowid
        assert stage() == 'interview_scheduled'
        assert client.post(f'/api/interviews/{interview}/complete').status_code == 200
        assert stage() == 'interviewed'
        offer = client.post(f'/api/applications/{app}/offers', json={'details': 'Fictional offer'})
        assert offer.status_code == 201, offer.text
        assert stage() == 'offered'
        login(client, 'candidate')
        assert client.get(endpoint).status_code == 403
        assert client.post(f"/api/offers/{offer.json()['id']}/respond", json={'decision': 'accepted'}).status_code == 200
        login(client, 'hr')
        assert client.post(f'/api/applications/{app}/decision', json={'decision': 'hire'}).status_code == 200
        assert stage() == 'hired'
        assert client.post('/api/auth/logout').status_code == 204
        assert client.get(endpoint).status_code == 401


def test_scheduling_only_does_not_disclose_status_stage(tmp_path):
    path = tmp_path / 'scope.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?, 'job-backend-python', 'fictional.pdf', 'Python')", (candidate,)).lastrowid
        response = client.post('/api/graph-reviews', json={'application_id': app, 'intent': 'scheduling'})
        assert response.status_code == 201
        assert 'candidate_progress' not in response.json()['state']


@pytest.mark.parametrize('change,expected', [('revocation', 401), ('stage', 409)])
def test_wal_concurrent_change_is_detected_before_return(tmp_path, monkeypatch, change, expected):
    from app import graph_orchestrator

    path = tmp_path / 'wal.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        with connect(path) as db:
            db.execute('PRAGMA journal_mode=WAL')
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?, 'job-backend-python', 'fictional.pdf', 'Python')", (candidate,)).lastrowid
        review = client.post('/api/graph-reviews', json={'application_id': app, 'intent': 'status'})
        assert review.status_code == 201
        started, resume = Event(), Event()
        original = graph_orchestrator.candidate_stage
        def stalled(*args):
            result = original(*args)
            if not started.is_set():
                started.set()
                assert resume.wait(15)
            return result
        monkeypatch.setattr(graph_orchestrator, 'candidate_stage', stalled)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.get, f"/api/graph-reviews/{review.json()['id']}")
            try:
                assert started.wait(15)
                with connect(path) as db:
                    if change == 'revocation':
                        db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='hr')")
                    else:
                        db.execute("UPDATE applications SET status='advanced' WHERE id=?", (app,))
            finally:
                resume.set()
            assert future.result(timeout=20).status_code == expected

"""Candidate feedback is explicitly released and bound to its current review."""
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401

from app.main import create_app


def test_publication_owner_freshness_and_retraction(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    with TestClient(app) as candidate, TestClient(app) as other, TestClient(app) as hr:
        login(candidate, 'candidate')
        login(other, 'candidate2')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        app_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes()).json()['id']
        endpoint = f'/api/applications/{app_id}/candidate-feedback'
        assert candidate.get(endpoint).json() == {'status': 'unavailable'}
        assert other.get(endpoint).status_code == 404
        assert hr.get(endpoint).status_code == 403
        source = hr.get(f'/api/applications/{app_id}/rubric-source').json()
        text = source['resume_text']
        start = next(i for i, char in enumerate(text) if not char.isspace())
        items = [{'category': name, 'score': None} for name in
                 ['mandatory_skills', 'preferred_skills', 'experience', 'projects', 'education']]
        items[0] = {'category': 'mandatory_skills', 'score': 75, 'start': start, 'end': start + 1}
        review = hr.post(f'/api/applications/{app_id}/rubric-assessment', json={'source_digest': source['source_digest'], 'items': items}).json()
        body = {'audit_event_id': review['audit_event_id'], 'explanation': 'Please provide more project detail.'}
        assert candidate.post(endpoint, json=body).status_code == 403
        assert hr.post(endpoint, json={**body, 'explanation': '  '}).status_code == 422
        assert hr.post(endpoint, json=body).status_code == 200
        visible = candidate.get(endpoint).json()
        assert visible['status'] == 'current'
        assert (visible['lower'], visible['upper']) == (30, 90)
        assert visible['explanation'] == body['explanation']
        assert 'items' not in visible and 'resume_text' not in visible and 'reviewer_id' not in visible
        assert other.get(endpoint).status_code == 404
        with db_for(mode) as db:
            details = db.execute("SELECT details FROM audit_events WHERE action='feedback_published'").fetchone()['details']
            assert body['explanation'] not in details
        # A new review supersedes the previous candidate-visible release.
        review2 = hr.post(f'/api/applications/{app_id}/rubric-assessment', json={'source_digest': source['source_digest'], 'items': items}).json()
        assert candidate.get(endpoint).json() == {'status': 'unavailable'}
        assert hr.post(endpoint, json=body).status_code == 409
        assert hr.post(endpoint, json={**body, 'audit_event_id': review2['audit_event_id']}).status_code == 200
        with db_for(mode) as db:
            db.execute("UPDATE applications SET resume_text=resume_text || ' changed' WHERE id=%s", (app_id,))
        assert candidate.get(endpoint).json() == {'status': 'unavailable'}
        assert candidate.delete(endpoint).status_code == 403
        with db_for(mode) as db:
            db.execute("UPDATE applications SET resume_text=repeat('x',12001) WHERE id=%s", (app_id,))
        assert hr.delete(endpoint).json()['status'] == 'retracted'
        assert hr.delete(endpoint).json()['status'] == 'unavailable'
    with TestClient(create_app(seed_demo=True)) as restarted:
        login(restarted, 'candidate')
        assert restarted.get(endpoint).json() == {'status': 'unavailable'}


def test_publication_and_audit_are_atomic(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as candidate, TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        app_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes()).json()['id']
        source = hr.get(f'/api/applications/{app_id}/rubric-source').json()
        items = [{'category': name, 'score': None} for name in ['mandatory_skills', 'preferred_skills', 'experience', 'projects', 'education']]
        start = next(i for i, char in enumerate(source['resume_text']) if not char.isspace())
        items[0] = {'category': 'mandatory_skills', 'score': 50, 'start': start, 'end': start + 1}
        event = hr.post(f'/api/applications/{app_id}/rubric-assessment', json={'source_digest': source['source_digest'], 'items': items}).json()['audit_event_id']
        with db_for(mode) as db:
            db.execute("ALTER TABLE audit_events ADD CONSTRAINT reject_feedback CHECK (action <> 'feedback_published')")
        endpoint = f'/api/applications/{app_id}/candidate-feedback'
        assert hr.post(endpoint, json={'audit_event_id': event, 'explanation': 'Synthetic feedback'}).status_code == 500
        assert candidate.get(endpoint).json() == {'status': 'unavailable'}


def test_publish_session_expiry_while_waiting_rolls_back(mode, monkeypatch):  # noqa: F811
    import time
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace

    from test_postgres_scheduling import wait_for_lock

    import app.postgres_experimental as pg_app

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        app_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes()).json()['id']
        source = hr.get(f'/api/applications/{app_id}/rubric-source').json()
        items = [{'category': name, 'score': None} for name in ['mandatory_skills', 'preferred_skills', 'experience', 'projects', 'education']]
        start = next(i for i, char in enumerate(source['resume_text']) if not char.isspace())
        items[0] = {'category': 'mandatory_skills', 'score': 50, 'start': start, 'end': start + 1}
        event = hr.post(f'/api/applications/{app_id}/rubric-assessment', json={'source_digest': source['source_digest'], 'items': items}).json()['audit_event_id']
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute('UPDATE applications SET status=status WHERE id=%s', (app_id,))
            future = pool.submit(hr.post, f'/api/applications/{app_id}/candidate-feedback', json={'audit_event_id': event, 'explanation': 'Synthetic feedback'})
            wait_for_lock(mode, 'transactionid')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=20).status_code == 401
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM candidate_rubric_feedback').fetchone()['n'] == 0
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='feedback_published'").fetchone()['n'] == 0

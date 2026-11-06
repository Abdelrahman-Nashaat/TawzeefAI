"""Verify a source-bound review against isolated PostgreSQL."""
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401

from app.main import create_app


def test_hr_review_and_stale_source(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        created = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes())
        assert created.status_code == 201
        app_id = created.json()['id']
        source_url = f'/api/applications/{app_id}/rubric-source'
        review_url = f'/api/applications/{app_id}/rubric-assessment'
        assert candidate.get(source_url).status_code == 403
        assert candidate.get(review_url).status_code == 403
        source = hr.get(source_url).json()
        text = source['resume_text']
        start = next(i for i, c in enumerate(text) if not c.isspace())
        names = ['mandatory_skills', 'preferred_skills', 'experience', 'projects', 'education']
        items = [{'category': key, 'score': None} for key in names]
        items[0] = {'category': names[0], 'score': 75, 'start': start, 'end': start + 1}
        invalid = [dict(item) for item in items]
        invalid[0]['end'] = len(text) + 1
        assert hr.post(review_url, json={'source_digest': source['source_digest'], 'items': invalid}).status_code == 422
        assert hr.post(review_url, json={'source_digest': source['source_digest'], 'items': [items[0]] * 5}).status_code == 422
        response = hr.post(review_url, json={'source_digest': source['source_digest'], 'items': items})
        assert response.status_code == 200, response.text
        assert response.json()['lower'] == 30
        assert response.json()['upper'] == 90
        assert hr.get(review_url).json()['items'] == response.json()['items']
        assert candidate.post(review_url, json={'source_digest': source['source_digest'], 'items': items}).status_code == 403
        with db_for(mode) as db:
            event = db.execute("SELECT details FROM audit_events WHERE application_id=%s AND action='rubric_reviewed'", (app_id,)).fetchone()
            assert text not in event['details']
            db.execute("UPDATE applications SET resume_text=resume_text || ' changed' WHERE id=%s", (app_id,))
        assert hr.get(review_url).status_code == 409
        assert hr.post(review_url, json={'source_digest': source['source_digest'], 'items': items}).status_code == 409
        assert candidate.get('/api/applications').json()[0]['status'] == 'submitted'


def test_audit_failure_rolls_back_and_job_change_invalidates(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as candidate, TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        app_id = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes()).json()['id']
        source_url = f'/api/applications/{app_id}/rubric-source'
        review_url = f'/api/applications/{app_id}/rubric-assessment'
        source = hr.get(source_url).json()
        items = [{'category': key, 'score': None} for key in ['mandatory_skills', 'preferred_skills', 'experience', 'projects', 'education']]
        body = {'source_digest': source['source_digest'], 'items': items}
        with db_for(mode) as db:
            db.execute("ALTER TABLE audit_events ADD CONSTRAINT reject_rubric CHECK (action <> 'rubric_reviewed')")
        assert hr.post(review_url, json=body).status_code == 500
        assert hr.get(review_url).status_code == 404
        with db_for(mode) as db:
            db.execute('ALTER TABLE audit_events DROP CONSTRAINT reject_rubric')
        assert hr.post(review_url, json=body).status_code == 200
        with db_for(mode) as db:
            db.execute("UPDATE jobs SET title=title || ' changed' WHERE id=%s", (job,))
        assert hr.get(review_url).status_code == 409
        assert hr.post(review_url, json=body).status_code == 409


def test_review_expiring_during_lock_wait_rolls_back(mode, monkeypatch):  # noqa: F811
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
        items = [{'category': key, 'score': None} for key in ['mandatory_skills', 'preferred_skills', 'experience', 'projects', 'education']]
        body = {'source_digest': source['source_digest'], 'items': items}
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute('UPDATE applications SET status=status WHERE id=%s', (app_id,))
            future = pool.submit(hr.post, f'/api/applications/{app_id}/rubric-assessment', json=body)
            wait_for_lock(mode, 'transactionid')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=20).status_code == 401
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM hr_rubric_assessments').fetchone()['n'] == 0
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='rubric_reviewed'").fetchone()['n'] == 0

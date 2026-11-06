"""Candidate lifecycle progress from real PostgreSQL records."""
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_postgres_cancellation_lifecycle import book
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401
from test_postgres_interview_completion import end_interview
from test_postgres_scheduling import wait_for_lock

from app.main import create_app


def test_progress_follows_actual_records_and_is_owner_only(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as other, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(other, 'candidate2')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        app = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes()).json()['id']
        path = f'/api/workflows/{app}/progress'
        def stage(expected):
            response = candidate.get(path)
            assert response.status_code == 200, response.text
            assert response.json() == {'application_id': app, 'stage': expected}
        assert other.get(path).status_code == 404
        assert other.get('/api/workflows/999999999/progress').status_code == 404
        assert hr.get(path).status_code == 403
        stage('applied')
        assert hr.post(f'/api/applications/{app}/screen').status_code == 200
        stage('screened')
        assert hr.post(f'/api/applications/{app}/decision', json={'decision': 'advance'}).status_code == 200
        stage('shortlisted')
        from datetime import UTC, datetime, timedelta
        start = datetime.now(UTC) + timedelta(days=1)
        slot = hr.post('/api/slots', json={'start_at': start.isoformat(), 'end_at': (start + timedelta(hours=1)).isoformat(), 'interviewer': 'Example'}).json()['id']
        interview = book(candidate, app, slot)
        stage('interview_scheduled')
        assert candidate.post(f'/api/interviews/{interview}/candidate-cancel').status_code == 200
        stage('shortlisted')
        interview = book(candidate, app, slot)
        end_interview(mode, interview)
        stage('interview_scheduled')  # Elapsed time does not confirm attendance.
        assert hr.post(f'/api/interviews/{interview}/complete').status_code == 200
        stage('interviewed')
        offer = hr.post(f'/api/applications/{app}/offers', json={'details': 'Terms'}).json()['id']
        stage('offered')
        assert candidate.post(f'/api/offers/{offer}/respond', json={'decision': 'accepted'}).status_code == 200
        assert hr.post(f'/api/applications/{app}/decision', json={'decision': 'hire'}).status_code == 200
        stage('hired')
    with TestClient(create_app(seed_demo=True)) as reopened:
        login(reopened, 'candidate')
        assert reopened.get(path).json()['stage'] == 'hired'


def test_rejection_stage_hides_hr_reason(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        app = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes()).json()['id']
        assert hr.post(f'/api/applications/{app}/decision', json={'decision': 'reject', 'note': 'Private HR reason'}).status_code == 200
        response = candidate.get(f'/api/workflows/{app}/progress')
        assert response.json() == {'application_id': app, 'stage': 'rejected'}


def test_expired_session_during_application_lock_returns_no_progress(mode, monkeypatch):  # noqa: F811
    import app.postgres_experimental as pg_app

    with TestClient(create_app(seed_demo=True)) as candidate:
        login(candidate, 'candidate')
        job = candidate.get('/api/jobs').json()[0]['id']
        app = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes()).json()['id']
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute('SELECT id FROM applications WHERE id=%s FOR UPDATE', (app,))
            future = pool.submit(candidate.get, f'/api/workflows/{app}/progress')
            wait_for_lock(mode, 'transactionid')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=20).status_code == 401

"""Submission notices remain private and acknowledgement belongs to each HR user."""
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401
from test_postgres_scheduling import wait_for_lock

from app.main import create_app


def test_submission_inbox_pagination_role_and_reader_isolation(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    with TestClient(app) as candidate, TestClient(app) as hr, TestClient(app) as other_hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        with db_for(mode) as db:
            db.execute("""INSERT INTO users(username,display_name,role,password_hash,active)
                SELECT 'hr2','Second HR','hr',password_hash,1 FROM users WHERE username='hr'""")
        assert other_hr.post('/api/auth/login', json={'username': 'hr2', 'password': 'hr-demo-only'}).status_code == 200
        job = candidate.get('/api/jobs').json()[0]['id']
        for _ in range(3):
            response = candidate.post('/api/applications', params={'job_id': job, 'filename': 'cv.docx'}, content=resume_bytes())
            assert response.status_code == 201, response.text
        endpoint = '/api/hr/submission-notices'
        assert candidate.get(endpoint).status_code == 403
        assert candidate.post(endpoint + '/1/read').status_code == 403
        first = hr.get(endpoint, params={'limit': 2}).json()
        assert len(first['items']) == 2 and first['next_before'] == first['items'][-1]['id']
        second = hr.get(endpoint, params={'limit': 2, 'before': first['next_before']}).json()
        assert len(second['items']) == 1 and second['next_before'] is None
        ids = [row['id'] for row in first['items'] + second['items']]
        assert ids == sorted(set(ids), reverse=True)
        assert all(set(item) == {'id', 'application_id', 'submitted_job_id', 'created_at', 'is_read'} for item in first['items'])
        assert hr.post(f'{endpoint}/{ids[0]}/read').json()['is_read'] is True
        assert hr.post(f'{endpoint}/{ids[0]}/read').status_code == 200
        assert hr.post(f'{endpoint}/99999/read').status_code == 404
        assert [item['is_read'] for item in hr.get(endpoint).json()['items']] == [True, False, False]
        assert not any(item['is_read'] for item in other_hr.get(endpoint).json()['items'])
    with TestClient(create_app(seed_demo=True)) as restarted:
        login(restarted, 'hr')
        assert [item['is_read'] for item in restarted.get(endpoint).json()['items']] == [True, False, False]


def test_expired_session_rolls_back_notice_acknowledgement(mode, monkeypatch):  # noqa: F811
    import app.postgres_experimental as pg_app

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        assert candidate.post('/api/applications', params={'job_id': job, 'filename': 'cv.docx'}, content=resume_bytes()).status_code == 201
        notice_id = hr.get('/api/hr/submission-notices').json()['items'][0]['id']
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute('SELECT id FROM hr_notifications WHERE id=%s FOR UPDATE', (notice_id,))
            future = pool.submit(hr.post, f'/api/hr/submission-notices/{notice_id}/read')
            wait_for_lock(mode, 'transactionid')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=20).status_code == 401
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM hr_notification_reads').fetchone()['n'] == 0


def test_role_revocation_before_authorization_denies_read(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        assert candidate.post('/api/applications', params={'job_id': job, 'filename': 'cv.docx'}, content=resume_bytes()).status_code == 201
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute("UPDATE users SET role='employee' WHERE username='hr'")
            future = pool.submit(hr.get, '/api/hr/submission-notices')
            wait_for_lock(mode, 'transactionid')
            blocker.commit()
            assert future.result(timeout=20).status_code == 403

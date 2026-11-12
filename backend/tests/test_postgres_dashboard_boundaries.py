"""Dashboard duration exclusions and session expiry against PostgreSQL."""
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401
from test_postgres_scheduling import wait_for_lock

from app.main import create_app


def test_dashboard_excludes_invalid_and_negative_hire_durations(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate')
        login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        ids = []
        for _ in range(3):
            response = candidate.post('/api/applications', params={'job_id': job, 'filename': 'cv.docx'}, content=resume_bytes())
            assert response.status_code == 201
            ids.append(response.json()['id'])
        with db_for(mode) as db:
            for app_id, submitted, hired in zip(ids, ['invalid', '2026-01-05T00:00:00Z', '2026-01-01T00:00:00Z'],
                                                ['2026-01-02T00:00:00Z', '2026-01-04T00:00:00Z', 'invalid'], strict=True):
                db.execute("UPDATE applications SET status='hired',created_at=%s WHERE id=%s", (submitted, app_id))
                db.execute("INSERT INTO audit_events(application_id,actor_id,action,details,created_at) SELECT %s,id,'decision',%s,%s FROM users WHERE username='hr'",
                           (app_id, '{"decision":"hire"}', hired))
        response = hr.get('/api/dashboard/jobs')
        assert response.status_code == 200, response.text
        item = next(row for row in response.json()['jobs'] if row['job_id'] == job)
        assert item['applications_total'] == 3
        assert item['pipeline'] == [{'status': 'hired', 'count': 3}]
        assert item['time_to_hire'] == {'average_days': None, 'count': 0}
        with db_for(mode) as db:
            db.execute("UPDATE applications SET created_at='2026-01-01T00:00:00Z' WHERE id=%s", (ids[0],))
        response = hr.get('/api/dashboard/jobs')
        assert response.status_code == 200
        item = next(row for row in response.json()['jobs'] if row['job_id'] == job)
        assert item['time_to_hire'] == {'average_days': 1.0, 'count': 1}


def test_dashboard_rechecks_session_expiry_after_lock_wait(mode, monkeypatch):  # noqa: F811
    import app.postgres_experimental as pg_app

    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute('LOCK TABLE jobs IN ACCESS EXCLUSIVE MODE')
            future = pool.submit(hr.get, '/api/dashboard/jobs')
            wait_for_lock(mode, 'relation')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=20).status_code == 401

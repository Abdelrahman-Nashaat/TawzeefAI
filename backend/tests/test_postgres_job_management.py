"""HR job management against an isolated PostgreSQL database."""
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401
from test_postgres_scheduling import wait_for_lock

from app.application_store import submit_application
from app.main import create_app


def payload():
    return {'id': 'synthetic-analyst', 'title': 'Synthetic analyst', 'location': 'Remote',
            'required_skills': ['Python'], 'preferred_skills': ['SQL'], 'responsibilities': ['Review data']}


def test_create_edit_and_preserve_review_basis_after_application(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as hr, TestClient(create_app(seed_demo=True)) as candidate:
        login(hr, 'hr')
        login(candidate, 'candidate')
        job = payload()
        assert candidate.post('/api/hr/jobs', json=job).status_code == 403
        assert candidate.get('/api/hr/jobs').status_code == 403
        response = hr.post('/api/hr/jobs', json=job)
        assert response.status_code == 201, response.text
        old = response.json()['revision']
        assert hr.post('/api/hr/jobs', json=job).status_code == 409
        job['required_skills'] = ['Python', 'Pandas']
        edited = hr.put('/api/hr/jobs/synthetic-analyst', json={'job': job, 'expected_revision': old})
        assert edited.status_code == 200, edited.text
        assert edited.json()['revision'] != old
        assert hr.put('/api/hr/jobs/synthetic-analyst', json={'job': job, 'expected_revision': old}).status_code == 409
        visible = next(j for j in candidate.get('/api/jobs').json() if j['id'] == job['id'])
        assert visible['required_skills'] == ['Python', 'Pandas']
        submitted = candidate.post('/api/applications', params={'job_id': job['id'], 'filename': 'resume.docx'}, content=resume_bytes())
        assert submitted.status_code == 201, submitted.text
        revision = edited.json()['revision']
        job['title'] = 'Changed role'
        assert hr.put('/api/hr/jobs/synthetic-analyst', json={'job': job, 'expected_revision': revision}).status_code == 409
        with db_for(mode) as db:
            assert [r['action'] for r in db.execute('SELECT action FROM job_audit ORDER BY id').fetchall()] == ['created', 'updated']
    with TestClient(create_app(seed_demo=True)) as restarted:
        login(restarted, 'hr')
        assert next(j for j in restarted.get('/api/hr/jobs').json() if j['id'] == job['id'])['required_skills'] == ['Python', 'Pandas']


def test_job_validation_and_audit_failure_are_atomic(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as hr:
        login(hr, 'hr')
        bad = payload()
        bad['required_skills'] = ['Python', 'python']
        assert hr.post('/api/hr/jobs', json=bad).status_code == 422
        with db_for(mode) as db:
            db.execute("ALTER TABLE job_audit ADD CONSTRAINT fail_creation CHECK(action <> 'created')")
        assert hr.post('/api/hr/jobs', json=payload()).status_code == 500
        with db_for(mode) as db:
            assert db.execute("SELECT 1 FROM jobs WHERE id='synthetic-analyst'").fetchone() is None


def test_concurrent_creation_only_one_succeeds(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as hr1, TestClient(create_app(seed_demo=True)) as hr2:
        login(hr1, 'hr')
        login(hr2, 'hr')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda client: client.post('/api/hr/jobs', json=payload()).status_code, (hr1, hr2)))
        assert sorted(results) == [201, 409]
        with db_for(mode) as db:
            assert db.execute("SELECT count(*) AS n FROM job_audit WHERE job_id='synthetic-analyst'").fetchone()['n'] == 1


def test_inflight_submission_prevents_job_edit(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        job = payload()
        revision = hr.post('/api/hr/jobs', json=job).json()['revision']
        job['title'] = 'Changed role'
        with db_for(mode) as submitting, ThreadPoolExecutor(max_workers=1) as pool:
            submitting.execute('SELECT id FROM jobs WHERE id=%s FOR SHARE', (job['id'],))
            future = pool.submit(hr.put, '/api/hr/jobs/synthetic-analyst', json={'job': job, 'expected_revision': revision})
            wait_for_lock(mode, 'transactionid')
            candidate = submitting.execute("SELECT id FROM users WHERE username='candidate'").fetchone()['id']
            submit_application(submitting, dialect='postgres', candidate_id=candidate, job_id=job['id'],
                               filename='cv.txt', resume_text='Synthetic CV', extraction_json=None)
            submitting.commit()
            assert future.result(timeout=20).status_code == 409
        with db_for(mode) as db:
            assert db.execute('SELECT title FROM jobs WHERE id=%s', (job['id'],)).fetchone()['title'] == 'Synthetic analyst'


def test_session_expiry_while_waiting_rolls_back_update(mode, monkeypatch):  # noqa: F811
    import app.postgres_experimental as pg_app

    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        job = payload()
        revision = hr.post('/api/hr/jobs', json=job).json()['revision']
        job['title'] = 'Changed role'
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute('SELECT id FROM jobs WHERE id=%s FOR UPDATE', (job['id'],))
            future = pool.submit(hr.put, '/api/hr/jobs/synthetic-analyst', json={'job': job, 'expected_revision': revision})
            wait_for_lock(mode, 'transactionid')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=20).status_code == 401
        with db_for(mode) as db:
            assert db.execute('SELECT title FROM jobs WHERE id=%s', (job['id'],)).fetchone()['title'] == 'Synthetic analyst'
            assert db.execute("SELECT count(*) AS n FROM job_audit WHERE action='updated'").fetchone()['n'] == 0


def test_role_revocation_before_commit_denies_creation(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute("UPDATE users SET role='employee' WHERE username='hr'")
            future = pool.submit(hr.post, '/api/hr/jobs', json=payload())
            wait_for_lock(mode, 'transactionid')
            blocker.commit()
            assert future.result(timeout=20).status_code == 403
        with db_for(mode) as db:
            assert db.execute("SELECT 1 FROM jobs WHERE id='synthetic-analyst'").fetchone() is None
            assert db.execute('SELECT count(*) AS n FROM job_audit').fetchone()['n'] == 0

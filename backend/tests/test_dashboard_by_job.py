"""Job-level dashboard metrics use recorded events and enforce HR access."""

import json
from contextlib import contextmanager

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def login(client, username):
    response = client.post('/api/auth/login', json={
        'username': username, 'password': f'{username}-demo-only',
    })
    assert response.status_code == 200


def test_job_metrics_count_recorded_events_and_valid_hire_durations(tmp_path):
    path = tmp_path / 'metrics.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            original = db.execute('SELECT * FROM jobs LIMIT 1').fetchone()
            db.execute('''INSERT INTO jobs(id,title,location,required_skills,preferred_skills,responsibilities)
                VALUES ('job-test-second','Second role',?,?,?,?)''',
                (original['location'], original['required_skills'], original['preferred_skills'],
                 original['responsibilities']))
            ids = []
            for job, status, created in [(original['id'], 'hired', '2026-01-01T00:00:00Z'),
                                         (original['id'], 'submitted', '2026-01-02T00:00:00Z'),
                                         ('job-test-second', 'hired', 'invalid')]:
                ids.append(db.execute('''INSERT INTO applications(candidate_id,job_id,filename,resume_text,status,created_at)
                    VALUES (?,?, 'private-file.docx','private resume marker',?,?)''',
                    (candidate, job, status, created)).lastrowid)
            for target in (ids[0], ids[0], ids[1], ids[2]):
                db.execute("INSERT INTO audit_events(application_id,actor_id,action,details) VALUES (?,?,'screened','{}')", (target, hr))
            db.execute("INSERT INTO audit_events(application_id,actor_id,action,details,created_at) VALUES (?,?,'decision',?,?)",
                       (ids[0], hr, json.dumps({'decision': 'hire'}), '2026-01-04T12:00:00Z'))
            db.execute("INSERT INTO audit_events(application_id,actor_id,action,details,created_at) VALUES (?,?,'decision',?,?)",
                       (ids[2], hr, json.dumps({'decision': 'hire'}), '2026-01-04T12:00:00Z'))

        login(client, 'hr')
        response = client.get('/api/dashboard/jobs')
        assert response.status_code == 200
        rows = {row['job_id']: row for row in response.json()['jobs']}
        first, second = rows[original['id']], rows['job-test-second']
        assert first['applications_total'] == 2
        assert first['screening_events'] == 3
        assert first['pipeline'] == [{'status': 'hired', 'count': 1}, {'status': 'submitted', 'count': 1}]
        assert first['time_to_hire']['average_days'] == 3.5
        assert first['time_to_hire']['count'] == 1
        assert second['applications_total'] == 1 and second['screening_events'] == 1
        assert second['time_to_hire']['average_days'] is None
        assert second['time_to_hire']['count'] == 0
        assert 'private resume marker' not in response.text and 'private-file.docx' not in response.text


def test_job_metrics_are_hr_only(tmp_path):
    with TestClient(create_app(tmp_path / 'metrics.sqlite3', seed_demo=True)) as client:
        assert client.get('/api/dashboard/jobs').status_code == 401
        for role in ('candidate', 'employee'):
            login(client, role)
            assert client.get('/api/dashboard/jobs').status_code == 403
        login(client, 'hr')
        rows = client.get('/api/dashboard/jobs').json()['jobs']
        assert rows and all(row['applications_total'] == 0 for row in rows)
        assert all(row['pipeline'] == [] and row['time_to_hire']['average_days'] is None for row in rows)


def test_job_metrics_use_one_database_snapshot(tmp_path, monkeypatch):
    from app import dashboard as module

    path = tmp_path / 'snapshot.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        with connect(path) as db:
            db.execute('PRAGMA journal_mode=WAL')
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            job = db.execute('SELECT id FROM jobs LIMIT 1').fetchone()[0]
        original_connect = module.connect

        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchall(self):
                rows = self.cursor.fetchall()
                with original_connect(path) as writer:
                    writer.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?,?,'x.docx','synthetic')", (candidate, job))
                return rows

        class Connection:
            def __init__(self, db):
                self.db = db

            def execute(self, query, *args):
                result = self.db.execute(query, *args)
                return Cursor(result) if query == 'SELECT id,title FROM jobs ORDER BY title,id' else result

        @contextmanager
        def instrumented_connect(db_path):
            with original_connect(db_path) as db:
                yield Connection(db)

        monkeypatch.setattr(module, 'connect', instrumented_connect)
        response = client.get('/api/dashboard/jobs')
        assert response.status_code == 200
        assert all(row['applications_total'] == 0 for row in response.json()['jobs'])
        with original_connect(path) as db:
            assert db.execute('SELECT count(*) FROM applications').fetchone()[0] == 1


def test_job_metrics_recheck_hr_session_before_return(tmp_path, monkeypatch):
    from app import dashboard as module

    with TestClient(create_app(tmp_path / 'revoked.sqlite3', seed_demo=True)) as client:
        login(client, 'hr')
        original = module._require_current_role
        calls = 0

        def check(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise HTTPException(status_code=401, detail='Session expired')
            return original(*args)

        monkeypatch.setattr(module, '_require_current_role', check)
        assert client.get('/api/dashboard/jobs').status_code == 401

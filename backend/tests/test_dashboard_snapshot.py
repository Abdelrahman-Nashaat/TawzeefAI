from contextlib import contextmanager

from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def test_dashboard_reads_one_snapshot_while_an_application_is_screened(tmp_path, monkeypatch):
    from app import dashboard as dashboard_module

    path = tmp_path / 'dashboard.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            db.execute('PRAGMA journal_mode=WAL')
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]

        original_connect = dashboard_module.connect

        def add_screened_application():
            with connect(path) as writer:
                app_id = writer.execute('''INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                    VALUES (?, 'job-backend-python', 'synthetic.docx', 'Synthetic')''', (candidate,)).lastrowid
                writer.execute("INSERT INTO audit_events(application_id,actor_id,action,details) VALUES (?,?,'screened','{}')", (app_id, hr))

        class Cursor:
            def __init__(self, cursor):
                self.cursor = cursor

            def fetchall(self):
                rows = self.cursor.fetchall()
                add_screened_application()
                return rows

        class Connection:
            def __init__(self, db):
                self.db = db

            def execute(self, query, *args):
                result = self.db.execute(query, *args)
                return Cursor(result) if query == 'SELECT id, status, created_at FROM applications' else result

        @contextmanager
        def instrumented_connect(db_path):
            with original_connect(db_path) as db:
                yield Connection(db)

        monkeypatch.setattr(dashboard_module, 'connect', instrumented_connect)
        report = client.get('/api/dashboard')
        assert report.status_code == 200
        assert report.json()['applications_total'] == 0
        assert report.json()['screening_events'] == 0
        assert all(row['count'] == 0 for row in report.json()['recorded_milestones'])
        assert all(row['count'] == 0 for row in report.json()['recorded_outcomes'])
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM applications').fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM audit_events WHERE action='screened'").fetchone()[0] == 1

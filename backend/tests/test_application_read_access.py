import pytest
from fastapi.testclient import TestClient

from app import recruitment
from app.db import connect
from app.main import create_app


@pytest.mark.parametrize('endpoint', ['list', 'detail'])
@pytest.mark.parametrize('change,expected', [
    ('revoke', 401), ('disable', 401), ('role', 403),
])
def test_hr_access_is_rechecked_before_returning_resume(tmp_path, monkeypatch, endpoint, change, expected):
    path = tmp_path / 'application-access.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            db.execute('PRAGMA journal_mode=WAL')
            candidate_id = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application_id = db.execute('''INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?, 'job-backend-python', 'private.docx', 'private resume content')''', (candidate_id,)).lastrowid
        original = recruitment.application_view
        changed = False

        def change_access(row, user):
            nonlocal changed
            result = original(row, user)
            if not changed:
                with connect(path) as db:
                    if change == 'revoke':
                        db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='hr')")
                    elif change == 'disable':
                        db.execute("UPDATE users SET active=0 WHERE username='hr'")
                    else:
                        db.execute("UPDATE users SET role='employee' WHERE username='hr'")
                changed = True
            return result

        monkeypatch.setattr(recruitment, 'application_view', change_access)
        url = '/api/applications' if endpoint == 'list' else f'/api/applications/{application_id}'
        response = client.get(url)
        assert changed and response.status_code == expected
        assert 'private resume content' not in response.text


@pytest.mark.parametrize('endpoint', ['list', 'detail'])
def test_candidate_does_not_receive_application_after_ownership_changes(tmp_path, monkeypatch, endpoint):
    path = tmp_path / 'application-owner.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
        with connect(path) as db:
            db.execute('PRAGMA journal_mode=WAL')
            candidate_id = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            employee_id = db.execute("SELECT id FROM users WHERE username='employee'").fetchone()[0]
            application_id = db.execute('''INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?, 'job-backend-python', 'private.docx', 'private resume content')''', (candidate_id,)).lastrowid
        original = recruitment.application_view
        changed = False

        def transfer_ownership(row, user):
            nonlocal changed
            result = original(row, user)
            if not changed:
                with connect(path) as db:
                    db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (employee_id, application_id))
                changed = True
            return result

        monkeypatch.setattr(recruitment, 'application_view', transfer_ownership)
        url = '/api/applications' if endpoint == 'list' else f'/api/applications/{application_id}'
        response = client.get(url)
        assert changed and response.status_code in (403, 404, 409)
        assert 'job-backend-python' not in response.text

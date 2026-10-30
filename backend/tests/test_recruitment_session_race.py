from io import BytesIO

import pytest
from docx import Document
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.mark.parametrize('change', ['revoke', 'role'])
def test_application_upload_rechecks_candidate_after_extraction(tmp_path, monkeypatch, change):
    from app import recruitment

    path = tmp_path / 'apply.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
        original = recruitment.extract_document_text

        def extract_then_revoke(filename, body):
            text = original(filename, body)
            with connect(path) as db:
                if change == 'revoke':
                    db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='candidate')")
                else:
                    db.execute("UPDATE users SET role='employee' WHERE username='candidate'")
            return text

        monkeypatch.setattr(recruitment, 'extract_document_text', extract_then_revoke)
        doc = Document()
        doc.add_paragraph('Synthetic Python developer')
        data = BytesIO()
        doc.save(data)
        response = client.post('/api/applications', params={'job_id': 'job-backend-python', 'filename': 'test.docx'}, content=data.getvalue())
        assert response.status_code == (401 if change == 'revoke' else 403)
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM applications').fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM audit_events WHERE action='submitted'").fetchone()[0] == 0


@pytest.mark.parametrize('change', ['revoke', 'role'])
def test_screen_rechecks_hr_in_write_transaction(tmp_path, monkeypatch, change):
    from app import recruitment

    path = tmp_path / 'screen.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute('''INSERT INTO applications(candidate_id,job_id,filename,resume_text,status)
                VALUES (?, 'job-backend-python', 'test.docx', 'Synthetic Python', 'submitted')''', (candidate,)).lastrowid
        original = recruitment.connect
        changed = False

        def revoke_before_write(database):
            nonlocal changed
            if not changed:
                changed = True
                with connect(path) as db:
                    if change == 'revoke':
                        db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='hr')")
                    else:
                        db.execute("UPDATE users SET role='employee' WHERE username='hr'")
            return original(database)

        monkeypatch.setattr(recruitment, 'connect', revoke_before_write)
        response = client.post(f'/api/applications/{application}/screen')
        assert changed and response.status_code == (401 if change == 'revoke' else 403)
        with connect(path) as db:
            assert db.execute('SELECT score_json FROM applications WHERE id=?', (application,)).fetchone()[0] is None
            assert db.execute('SELECT count(*) FROM audit_events WHERE application_id=?', (application,)).fetchone()[0] == 0

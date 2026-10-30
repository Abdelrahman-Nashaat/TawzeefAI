from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.mark.parametrize('change', ['revoke', 'role'])
def test_decision_rechecks_session_within_transaction(tmp_path, monkeypatch, change):
    from app import recruitment

    path = tmp_path / 'race.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={
            'username': 'hr', 'password': 'hr-demo-only',
        }).status_code == 200
        now = datetime.now(UTC) + timedelta(days=3)
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            application = db.execute('''INSERT INTO applications(candidate_id,job_id,filename,resume_text,status)
                VALUES (?, 'job-backend-python', 'synthetic.docx', 'Synthetic', 'advanced')''', (candidate,)).lastrowid
            db.execute('''INSERT INTO interviews(application_id,candidate_id,start_at,end_at,interviewer,interviewer_key,status,created_by)
                VALUES (?, ?, ?, ?, 'Synthetic', 'synthetic', 'scheduled', ?)''',
                (application, candidate, now.isoformat(), (now + timedelta(hours=1)).isoformat(), hr))
        original_connect = recruitment.connect
        changed = False

        def race_connect(database):
            nonlocal changed
            if not changed:
                changed = True
                with connect(path) as db:
                    if change == 'revoke':
                        db.execute("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username='hr')")
                    else:
                        db.execute("UPDATE users SET role='employee' WHERE username='hr'")
            return original_connect(database)

        monkeypatch.setattr(recruitment, 'connect', race_connect)
        response = client.post(f'/api/applications/{application}/decision', json={'decision': 'reject', 'note': 'Synthetic'})
        assert changed and response.status_code == (401 if change == 'revoke' else 403)
        with connect(path) as db:
            assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'advanced'
            assert db.execute('SELECT status FROM interviews WHERE application_id=?', (application,)).fetchone()[0] == 'scheduled'
            assert db.execute('SELECT count(*) FROM audit_events WHERE application_id=?', (application,)).fetchone()[0] == 0

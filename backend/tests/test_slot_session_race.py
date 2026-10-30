from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.mark.parametrize('action', ['create', 'book'])
@pytest.mark.parametrize('change', ['revoke', 'role'])
def test_slot_write_rechecks_session_and_role_before_mutation(tmp_path, monkeypatch, action, change):
    """A change after request authentication must prevent the actual write."""
    from app import slots

    path = tmp_path / 'race.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        def login(role):
            assert client.post('/api/auth/login', json={
                'username': role, 'password': f'{role}-demo-only',
            }).status_code == 200

        login('hr')
        starts = datetime.now(UTC) + timedelta(days=3)
        payload = {'start_at': starts.isoformat(), 'end_at': (starts + timedelta(hours=1)).isoformat(),
                   'interviewer': 'Synthetic HR'}
        slot_id = client.post('/api/slots', json=payload).json()['id']
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute('''INSERT INTO applications(candidate_id,job_id,filename,resume_text,status)
                VALUES (?, 'job-backend-python', 'synthetic.docx', 'Synthetic', 'advanced')''', (candidate,)).lastrowid
        role = 'hr' if action == 'create' else 'candidate'
        if action == 'book':
            login(role)
        original_connect = slots.connect
        changed = False

        def race_connect(database):
            nonlocal changed
            if not changed:
                changed = True
                with connect(path) as db:
                    if change == 'revoke':
                        db.execute('DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)', (role,))
                    else:
                        db.execute("UPDATE users SET role='employee' WHERE username=?", (role,))
            return original_connect(database)

        monkeypatch.setattr(slots, 'connect', race_connect)
        if action == 'create':
            response = client.post('/api/slots', json=payload)
        else:
            response = client.post(f'/api/slots/{slot_id}/book', json={'application_id': application})
        assert changed and response.status_code in (401, 403)
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 1
            assert db.execute('SELECT count(*) FROM interviews').fetchone()[0] == 0

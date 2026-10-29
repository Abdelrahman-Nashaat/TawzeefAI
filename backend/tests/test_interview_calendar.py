from datetime import UTC, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.auth import hash_password
from app.db import connect
from app.interview_calendar import router
from app.main import create_app


def login(client, name, password=None):
    assert client.post('/api/auth/login', json={'username': name, 'password': password or f'{name}-demo-only'}).status_code == 200


def test_calendar_utc_escaping_authorization_and_cancellation(tmp_path):
    app = create_app(tmp_path / 'calendar.sqlite3', seed_demo=True)
    app.include_router(router)
    with TestClient(app) as client:
        assert client.get('/api/interviews/1/calendar').status_code == 401
        login(client, 'candidate')
        with connect(app.state.database_path) as db:
            uid = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute('INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?,?,?,?)',
                                (uid, 'job-backend-python', 'private.pdf', 'Private resume')).lastrowid
            db.execute('INSERT INTO users(username,display_name,role,password_hash) VALUES (?,?,?,?)',
                       ('other', 'Other', 'candidate', hash_password('other-password')))
            db.execute('UPDATE jobs SET title=? WHERE id=?',
                       ('Engineering, Python; safely\\done\r\nBEGIN:VEVENT\nX-TEST:bad ' + 'م' * 100, 'job-backend-python'))
        start = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
        login(client, 'hr')
        cairo = timezone(timedelta(hours=2))
        payload = {'application_id': app_id, 'start_at': start.astimezone(cairo).isoformat(),
                   'end_at': (start + timedelta(hours=1)).astimezone(cairo).isoformat(), 'interviewer': 'PRIVATE NAME'}
        created = client.post('/api/interviews', json=payload)
        assert created.status_code == 201
        interview_id = created.json()['id']
        endpoint = f'/api/interviews/{interview_id}/calendar'
        response = client.get(endpoint)
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/calendar')
        assert response.headers['cache-control'] == 'no-store'
        raw = response.content
        assert raw.endswith(b'\r\n') and b'\n' not in raw.replace(b'\r\n', b'')
        assert all(len(line) <= 75 for line in raw.split(b'\r\n'))
        text = raw.decode('utf-8').replace('\r\n ', '')
        assert text.count('BEGIN:VEVENT') == 2  # One is escaped inside SUMMARY.
        assert text.count('\r\nBEGIN:VEVENT\r\n') == 1
        assert '\\nBEGIN:VEVENT\\nX-TEST:bad' in text
        assert 'Engineering\\, Python\\; safely\\\\done' in text
        assert f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}" in text
        assert 'PRIVATE NAME' not in text and 'Private resume' not in text
        login(client, 'candidate')
        assert client.get(endpoint).content == raw
        login(client, 'other', 'other-password')
        assert client.get(endpoint).status_code == 404
        login(client, 'employee')
        assert client.get(endpoint).status_code == 403
        login(client, 'hr')
        assert client.post(f'/api/interviews/{interview_id}/cancel').status_code == 200
        assert client.get(endpoint).status_code == 409
        login(client, 'candidate')
        assert client.get(endpoint).status_code == 409


def test_fractional_appointment_not_misrepresented(tmp_path):
    app = create_app(tmp_path / 'calendar.sqlite3', seed_demo=True)
    app.include_router(router)
    with TestClient(app) as client:
        login(client, 'candidate')
        with connect(app.state.database_path) as db:
            uid = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app_id = db.execute('INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?,?,?,?)',
                                (uid, 'job-backend-python', 'cv.pdf', 'text')).lastrowid
        login(client, 'hr')
        start = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=500000)
        created = client.post('/api/interviews', json={'application_id': app_id, 'start_at': start.isoformat(),
            'end_at': (start + timedelta(hours=1)).isoformat(), 'interviewer': 'HR'})
        assert created.status_code == 201
        assert client.get(f"/api/interviews/{created.json()['id']}/calendar").status_code == 409

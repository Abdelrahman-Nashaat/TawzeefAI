from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def test_hr_cannot_schedule_multiple_interviews_for_one_application(tmp_path):
    path = tmp_path / 'interviews.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as first, TestClient(app) as second:
        for client in (first, second):
            assert client.post('/api/auth/login', json={
                'username': 'hr', 'password': 'hr-demo-only',
            }).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications
                (candidate_id,job_id,filename,resume_text,status)
                VALUES (?, 'job-backend-python', 'test.docx', 'Synthetic', 'advanced')""", (candidate,)).lastrowid
        start = datetime.now(UTC) + timedelta(days=2)
        def payload(hours):
            begins = start + timedelta(hours=hours)
            return {'application_id': application, 'start_at': begins.isoformat(),
                    'end_at': (begins + timedelta(minutes=30)).isoformat(), 'interviewer': f'HR {hours}'}
        # Even nonoverlapping requests must not create two scheduled interviews.
        with ThreadPoolExecutor(max_workers=2) as executor:
            requests = [executor.submit(client.post, '/api/interviews', json=payload(hours))
                        for client, hours in ((first, 0), (second, 2))]
            responses = [request.result() for request in requests]
        assert sorted(response.status_code for response in responses) == [201, 409]
        created = next(response.json() for response in responses if response.status_code == 201)
        assert first.post(f"/api/interviews/{created['id']}/cancel").status_code == 200
        assert first.post('/api/interviews', json=payload(4)).status_code == 201
        with connect(path) as db:
            assert db.execute("SELECT count(*) FROM interviews WHERE application_id=? AND status='scheduled'",
                              (application,)).fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM audit_events WHERE application_id=? AND action='interview_scheduled'",
                              (application,)).fetchone()[0] == 2

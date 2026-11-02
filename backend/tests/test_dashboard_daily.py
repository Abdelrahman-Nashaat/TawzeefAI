"""Daily screening activity reports retained audit facts without exposing CVs."""

from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def login(client, username):
    assert client.post('/api/auth/login', json={
        'username': username, 'password': f'{username}-demo-only',
    }).status_code == 200


def test_daily_screenings_distinguishes_events_and_applications_at_utc_boundaries(tmp_path):
    path = tmp_path / 'daily.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            job = db.execute('SELECT id FROM jobs LIMIT 1').fetchone()[0]
            ids = [db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                    VALUES (?,?,'private-file.pdf','private-resume-marker')""", (candidate, job)).lastrowid
                   for _ in range(2)]
            events = [
                (ids[0], 'screened', '2026-04-30T23:59:59Z'),
                (ids[0], 'screened', '2026-05-01T00:00:00Z'),
                (ids[0], 'screened', '2026-05-01T01:00:00Z'),
                (ids[1], 'screened', '2026-05-01T02:00:00+02:00'),
                (ids[1], 'screened', '2026-05-02T00:00:00Z'),
                (ids[1], 'provisional_screened', '2026-05-01T03:00:00Z'),
                (ids[1], 'screened', 'not-a-date'),
            ]
            for application, action, timestamp in events:
                db.execute("""INSERT INTO audit_events(application_id,actor_id,action,details,created_at)
                              VALUES (?,?,?,'{}',?)""", (application, hr, action, timestamp))
        login(client, 'hr')
        response = client.get('/api/dashboard/screenings/daily', params={'end_date': '2026-05-02', 'days': 3})
        assert response.status_code == 200
        report = response.json()
        assert report['timezone'] == 'UTC'
        assert report['days'] == [
            {'date': '2026-04-30', 'screening_events': 1, 'applications_screened': 1},
            {'date': '2026-05-01', 'screening_events': 3, 'applications_screened': 2},
            {'date': '2026-05-02', 'screening_events': 1, 'applications_screened': 1},
        ]
        assert 'private-resume-marker' not in response.text
        assert 'private-file.pdf' not in response.text
        assert client.get('/api/dashboard/screenings/daily', params={'days': 91}).status_code == 422
        assert client.get('/api/dashboard/screenings/daily', params={'days': 0}).status_code == 422
        assert client.get('/api/dashboard/screenings/daily', params={'days': 2, 'end_date': '0001-01-01'}).status_code == 422


def test_daily_screenings_require_hr_and_zero_fill(tmp_path):
    with TestClient(create_app(tmp_path / 'daily.sqlite3', seed_demo=True)) as client:
        assert client.get('/api/dashboard/screenings/daily').status_code == 401
        for role in ('candidate', 'employee'):
            login(client, role)
            assert client.get('/api/dashboard/screenings/daily').status_code == 403
        login(client, 'hr')
        data = client.get('/api/dashboard/screenings/daily', params={'days': 2, 'end_date': '2026-05-01'}).json()
        assert data['days'] == [
            {'date': '2026-04-30', 'screening_events': 0, 'applications_screened': 0},
            {'date': '2026-05-01', 'screening_events': 0, 'applications_screened': 0},
        ]

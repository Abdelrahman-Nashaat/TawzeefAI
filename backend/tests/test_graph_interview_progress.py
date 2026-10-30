from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def test_graph_scheduler_reflects_interview_completion(tmp_path):
    path = tmp_path / 'graph-progress.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            app_id = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text,status)
                VALUES (?, 'job-backend-python', 'synthetic.docx', 'Synthetic', 'advanced')""", (candidate,)).lastrowid
            interview_id = db.execute("""INSERT INTO interviews (application_id,candidate_id,start_at,end_at,
                interviewer,interviewer_key,status,created_by) VALUES (?, ?, '2020-01-01T10:00:00.000000Z',
                '2020-01-01T11:00:00.000000Z', 'Synthetic', 'synthetic', 'scheduled', ?)""",
                (app_id, candidate, hr)).lastrowid
        review = client.post('/api/graph-reviews', json={'application_id': app_id, 'intent': 'scheduling'})
        assert review.status_code == 201
        before = review.json()['state']['scheduling']
        assert before['interview_completed'] is False and 'scheduled interview' in before['notice']
        assert client.post(f'/api/interviews/{interview_id}/complete').status_code == 200
        after = client.get(f"/api/graph-reviews/{review.json()['id']}").json()['state']['scheduling']
        assert after['interview_completed'] is True and 'interview was completed' in after['notice']
        assert after['eligible'] is True and after['available_slots'] == []
        with connect(path) as db:
            db.execute("""INSERT INTO interviews (application_id,candidate_id,start_at,end_at,
                interviewer,interviewer_key,status,created_by) VALUES (?, ?, '2099-01-01T10:00:00.000000Z',
                '2099-01-01T11:00:00.000000Z', 'Synthetic', 'synthetic', 'scheduled', ?)""",
                (app_id, candidate, hr))
        current = client.get(f"/api/graph-reviews/{review.json()['id']}").json()['state']['scheduling']
        assert current['interview_completed'] is True and current['eligible'] is False
        assert 'scheduled interview' in current['notice']

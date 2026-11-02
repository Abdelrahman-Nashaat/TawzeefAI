"""Dashboard milestones reflect recorded events, including later reversals."""

import json
from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def login(client, username):
    return client.post('/api/auth/login', json={'username': username, 'password': f'{username}-demo-only'})


def test_milestone_events_are_distinct_and_legacy_data_is_not_inferred(tmp_path):
    path = tmp_path / 'dashboard.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            app_ids = [db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text,status)
                VALUES (?, 'job-backend-python', 'synthetic.docx', 'Synthetic', ?)""",
                (candidate, status)).lastrowid for status in ('rejected', 'advanced', 'hired')]

            def event(application, action, details='{}'):
                db.execute('INSERT INTO audit_events(application_id,actor_id,action,details) VALUES (?,?,?,?)',
                           (application, hr, action, details))

            first, second, legacy = app_ids
            for _ in range(2):
                event(first, 'screened')
                event(first, 'interview_scheduled')
            event(first, 'interview_cancelled')
            event(first, 'decision', json.dumps({'decision': 'reject'}))
            event(second, 'screened')
            event(second, 'decision', json.dumps({'decision': 'advance'}))
            event(second, 'interview_completed')
            event(second, 'offer_presented')
            event(second, 'offer_declined')
            event(second, 'offer_presented')
            event(second, 'offer_accepted')
            event(second, 'offer_withdrawn')
            event(second, 'decision', '{invalid')
            event(second, 'decision', json.dumps({'decision': ['advance']}))
            # A current hired status without a hire audit event is not a historical event.
            assert legacy != second

        assert login(client, 'candidate').status_code == 200
        assert client.get('/api/dashboard').status_code == 403
        assert login(client, 'hr').status_code == 200
        response = client.get('/api/dashboard')
        assert response.status_code == 200
        report = response.json()
        assert report['screening_events'] == 3
        assert {row['stage']: row['count'] for row in report['recorded_milestones']} == {
            'received': 3, 'screened': 2, 'advanced': 1, 'interview_scheduled': 1,
            'interview_completed': 1, 'offer_presented': 1, 'hired': 0,
        }
        assert {row['outcome']: row['count'] for row in report['recorded_outcomes']} == {
            'interview_cancelled': 1, 'offer_accepted': 1, 'offer_declined': 1, 'offer_withdrawn': 1, 'rejected': 1,
        }
        assert {row['status']: row['count'] for row in report['pipeline']} == {
            'advanced': 1, 'hired': 1, 'rejected': 1,
        }


def test_real_cancelled_interview_and_rejection_remain_in_history(tmp_path):
    path = tmp_path / 'dashboard.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        assert login(client, 'candidate').status_code == 200
        document = Document()
        document.add_paragraph('Synthetic Python SQL REST API')
        stream = BytesIO()
        document.save(stream)
        application = client.post('/api/applications', params={'job_id': 'job-backend-python',
                                  'filename': 'synthetic.docx'}, content=stream.getvalue())
        assert application.status_code == 201
        app_id = application.json()['id']
        assert login(client, 'hr').status_code == 200
        assert client.post(f'/api/applications/{app_id}/screen').status_code == 200
        assert client.post(f'/api/applications/{app_id}/screen').status_code == 200
        interview = client.post('/api/interviews', json={'application_id': app_id,
            'start_at': '2030-01-02T11:00:00Z', 'end_at': '2030-01-02T12:00:00Z', 'interviewer': 'Synthetic'})
        assert interview.status_code == 201
        assert client.post(f"/api/interviews/{interview.json()['id']}/cancel").status_code == 200
        assert client.post(f'/api/applications/{app_id}/decision', json={'decision': 'reject', 'note': 'Reviewed'}).status_code == 200
        report = client.get('/api/dashboard').json()
        counts = {row['stage']: row['count'] for row in report['recorded_milestones']}
        assert counts['screened'] == 1 and counts['interview_scheduled'] == 1
        assert report['screening_events'] == 2
        assert {row['outcome']: row['count'] for row in report['recorded_outcomes']}['rejected'] == 1
        assert report['pipeline'] == [{'status': 'rejected', 'count': 1}]

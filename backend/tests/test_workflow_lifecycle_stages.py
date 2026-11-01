"""The workflow reports actionable stages derived from persisted records."""

from datetime import UTC, datetime, timedelta
from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient

from app import recruitment, scheduling, workflow
from app.db import connect
from app.main import create_app


def test_hr_workflow_tracks_interview_and_offer_lifecycle(tmp_path, monkeypatch):
    database = tmp_path / 'stages.sqlite3'
    document = Document()
    document.add_paragraph('Python and SQL')
    cv = BytesIO()
    document.save(cv)
    with TestClient(create_app(database, seed_demo=True)) as client:
        def login(role):
            assert client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'}).status_code == 200

        login('candidate')
        response = client.post('/api/applications', params={'job_id': 'job-backend-python', 'filename': 'cv.docx'}, content=cv.getvalue())
        assert response.status_code == 201
        application = response.json()['id']
        endpoint = f'/api/workflows/{application}'
        assert client.get(endpoint).status_code == 403
        login('hr')
        assert client.get(endpoint).json()['next_step'] == 'screen'
        assert client.post(endpoint + '/run').status_code == 200
        assert client.get(endpoint).json()['next_step'] == 'human_review'
        assert client.post(f'/api/applications/{application}/decision', json={'decision': 'advance'}).status_code == 200
        assert client.get(endpoint).json()['next_step'] == 'schedule_interview'

        start = datetime.now(UTC) + timedelta(days=2)
        end = start + timedelta(minutes=30)
        response = client.post('/api/interviews', json={'application_id': application, 'start_at': start.isoformat(),
                                                        'end_at': end.isoformat(), 'interviewer': 'Example HR'})
        assert response.status_code == 201, response.text
        interview = response.json()['id']
        assert client.get(endpoint).json()['next_step'] == 'await_interview'

        class DuringInterview(datetime):
            @classmethod
            def now(cls, tz=None):
                instant = start + timedelta(minutes=15)
                return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

        monkeypatch.setattr(workflow, 'datetime', DuringInterview)
        assert client.get(endpoint).json()['next_step'] == 'await_interview'

        class AfterInterview(datetime):
            @classmethod
            def now(cls, tz=None):
                instant = end + timedelta(minutes=1)
                return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

        monkeypatch.setattr(workflow, 'datetime', AfterInterview)
        monkeypatch.setattr(scheduling, 'datetime', AfterInterview)
        monkeypatch.setattr(recruitment, 'datetime', AfterInterview)
        assert client.get(endpoint).json()['next_step'] == 'confirm_interview'
        assert client.post(f'/api/interviews/{interview}/complete').status_code == 200
        assert client.get(endpoint).json()['next_step'] == 'prepare_offer'
        response = client.post(f'/api/applications/{application}/offers', json={'details': 'Example offer'})
        assert response.status_code == 201, response.text
        offer = response.json()['id']
        assert client.get(endpoint).json()['next_step'] == 'await_offer_response'
        login('candidate')
        assert client.get(endpoint).status_code == 403
        assert client.post(f'/api/offers/{offer}/respond', json={'decision': 'declined'}).status_code == 200
        login('hr')
        assert client.get(endpoint).json()['next_step'] == 'review_offer'
        response = client.post(f'/api/applications/{application}/offers', json={'details': 'Revised example offer'})
        assert response.status_code == 201, response.text
        offer = response.json()['id']
        assert client.get(endpoint).json()['next_step'] == 'await_offer_response'
        login('candidate')
        assert client.post(f'/api/offers/{offer}/respond', json={'decision': 'accepted'}).status_code == 200
        login('hr')
        assert client.get(endpoint).json()['next_step'] == 'finalize_hire'
        assert client.post(f'/api/applications/{application}/decision', json={'decision': 'hire'}).status_code == 200
        assert client.get(endpoint).json()['next_step'] == 'complete'


def test_inconsistent_offer_and_interview_data_require_review(tmp_path):
    database = tmp_path / 'bad-stage.sqlite3'
    with TestClient(create_app(database, seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(database) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            application = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status,score_json) VALUES (?,'job-backend-python','x.docx','Python','advanced','{}')", (candidate,)).lastrowid
            interview = db.execute("INSERT INTO interviews(application_id,candidate_id,start_at,end_at,interviewer,interviewer_key,created_by) VALUES (?,?,'bad','bad','HR','hr',?)", (application, candidate, hr)).lastrowid
        endpoint = f'/api/workflows/{application}'
        assert client.get(endpoint).json()['next_step'] == 'review_interview'
        with connect(database) as db:
            db.execute("UPDATE interviews SET status='cancelled' WHERE id=?", (interview,))
            db.execute("UPDATE applications SET status='offered' WHERE id=?", (application,))
        assert client.get(endpoint).json()['next_step'] == 'review_offer'

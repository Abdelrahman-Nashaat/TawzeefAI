"""Synthetic end-to-end recruitment journey through the public API."""

from datetime import UTC, datetime, timedelta
from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient

from app import recruitment, scheduling
from app.db import connect
from app.main import create_app


def test_reviewed_application_to_hire(tmp_path, monkeypatch):
    database = tmp_path / 'lifecycle.sqlite3'
    app = create_app(database, seed_demo=True)
    document = Document()
    document.add_paragraph('Python SQL REST APIs')
    data = BytesIO()
    document.save(data)

    with TestClient(app) as client:
        def login(role):
            response = client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'})
            assert response.status_code == 200, response.text

        def approve(review):
            proposal = review['state']['action_proposal']
            response = client.post(f"/api/graph-reviews/{review['id']}/resume", json={
                'reviewed': True, 'approval_fingerprint': proposal['approval_fingerprint'],
            })
            assert response.status_code == 200, response.text
            return response.json()['state']['action_result']

        login('candidate')
        response = client.post('/api/applications', params={'job_id': 'job-backend-python', 'filename': 'resume.docx'},
                               content=data.getvalue(), headers={'content-type': 'application/octet-stream'})
        assert response.status_code == 201, response.text
        application_id = response.json()['id']
        assert client.post(f'/api/applications/{application_id}/screen').status_code == 403

        login('hr')
        assert client.post(f'/api/applications/{application_id}/screen').status_code == 200
        criterion_response = client.post('/api/jobs/job-backend-python/rubric/criteria',
                                         json={'category': 'mandatory', 'label': 'Python'})
        assert criterion_response.status_code == 201, criterion_response.text
        criterion = criterion_response.json()['criterion_ref']
        evidence_response = client.post(f'/api/applications/{application_id}/rubric/evidence', json={'start': 0, 'end': 6})
        assert evidence_response.status_code == 201, evidence_response.text
        evidence = evidence_response.json()['evidence_ref']
        response = client.post(f'/api/applications/{application_id}/rubric/assessment', json={'judgments': [
            {'criterion_ref': criterion, 'status': 'met', 'evidence_refs': [evidence]},
        ]})
        assert response.status_code == 201, response.text
        response = client.post('/api/graph-reviews', json={'application_id': application_id, 'intent': 'advance_application'})
        assert response.status_code == 201, response.text
        review = response.json()
        assert review['state']['matching_specialist']['status'] == 'current'
        assert approve(review) == {'status': 'advanced'}

        start = datetime.now(UTC) + timedelta(days=2)
        end = start + timedelta(minutes=30)
        response = client.post('/api/graph-reviews', json={
            'application_id': application_id, 'intent': 'create_private_slot',
            'slot': {'start_at': start.isoformat(), 'end_at': end.isoformat(), 'interviewer': 'Synthetic interviewer'},
        })
        assert response.status_code == 201, response.text
        slot = approve(response.json())['slot']['id']

        login('candidate')
        assert client.get(f'/api/applications/{application_id}').json()['status'] == 'advanced'
        assert client.get(f'/api/applications/{application_id}/screening-history').status_code == 403
        assert [item['id'] for item in client.get('/api/slots', params={'application_id': application_id}).json()] == [slot]
        response = client.post(f'/api/slots/{slot}/book', json={'application_id': application_id})
        assert response.status_code == 201, response.text
        interview = response.json()['id']
        assert client.post(f'/api/interviews/{interview}/complete').status_code == 403

        # Advance only application clocks; no interview data or timestamps are rewritten.
        later = end + timedelta(minutes=1)
        class AfterInterview(datetime):
            @classmethod
            def now(cls, tz=None):
                return later.astimezone(tz) if tz else later.replace(tzinfo=None)

        monkeypatch.setattr(scheduling, 'datetime', AfterInterview)
        monkeypatch.setattr(recruitment, 'datetime', AfterInterview)
        login('hr')
        response = client.post(f'/api/interviews/{interview}/complete')
        assert response.status_code == 200, response.text
        response = client.post(f'/api/applications/{application_id}/offers', json={'details': 'Synthetic offer'})
        assert response.status_code == 201, response.text
        offer = response.json()['id']
        assert client.post(f'/api/applications/{application_id}/decision', json={'decision': 'hire'}).status_code == 409
        login('candidate')
        assert client.post(f'/api/offers/{offer}/respond', json={'decision': 'accepted'}).status_code == 200
        login('hr')
        assert client.post(f'/api/applications/{application_id}/decision', json={'decision': 'hire'}).status_code == 200
        assert client.get(f'/api/applications/{application_id}').json()['status'] == 'hired'
        with connect(database) as db:
            actions = [row[0] for row in db.execute('SELECT action FROM audit_events WHERE application_id=?', (application_id,))]
            assert {'screened', 'decision', 'interview_completed', 'offer_presented', 'offer_accepted'} <= set(actions)

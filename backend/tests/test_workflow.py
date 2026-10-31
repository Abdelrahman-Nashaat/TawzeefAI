from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient

from app.main import create_app


def test_workflow_screens_once_then_waits_for_hr_and_scheduling(tmp_path, accepted_offer):
    with TestClient(create_app(tmp_path / 'workflow.sqlite3', seed_demo=True)) as client:
        def login(role):
            assert client.post('/api/auth/login', json={
                'username': role, 'password': f'{role}-demo-only',
            }).status_code == 200

        login('candidate')
        document = Document()
        document.add_paragraph('Built Python services, SQL databases and REST APIs.')
        file = BytesIO()
        document.save(file)
        application = client.post('/api/applications', params={
            'job_id': 'job-backend-python', 'filename': 'resume.docx',
        }, content=file.getvalue()).json()
        path = f"/api/workflows/{application['id']}"
        assert client.get(path).status_code == 403
        assert client.post(path + '/run').status_code == 403

        login('hr')
        assert client.get(path).json()['next_step'] == 'screen'
        screened = client.post(path + '/run')
        assert screened.status_code == 200
        assert screened.json()['last_step'] == 'screen'
        assert screened.json()['next_step'] == 'human_review'
        assert client.post(path + '/run').json()['next_step'] == 'human_review'
        assert sum(e['action'] == 'screened' for e in client.get('/api/audit').json()) == 1

        assert client.post(f"/api/applications/{application['id']}/decision", json={'decision': 'advance'}).status_code == 200
        assert client.get(path).json()['next_step'] == 'schedule_interview'
        created = client.post('/api/interviews', json={
            'application_id': application['id'], 'start_at': '2099-01-01T10:00:00Z',
            'end_at': '2099-01-01T11:00:00Z', 'interviewer': 'Demo HR',
        })
        assert created.status_code == 201
        assert client.get(path).json()['next_step'] == 'human_decision'
        assert client.post(f"/api/interviews/{created.json()['id']}/cancel").status_code == 200
        assert client.get(path).json()['next_step'] == 'schedule_interview'
        second = client.post('/api/interviews', json={
            'application_id': application['id'], 'start_at': '2099-01-01T10:00:00Z',
            'end_at': '2099-01-01T11:00:00Z', 'interviewer': 'Demo HR',
        })
        assert second.status_code == 201
        assert client.post(f"/api/interviews/{second.json()['id']}/cancel").status_code == 200
        accepted_offer(client, application['id'])
        assert client.post(f"/api/applications/{application['id']}/decision", json={'decision': 'hire'}).status_code == 200
        assert client.get(path).json()['next_step'] == 'complete'
        assert client.post(path + '/run').status_code == 409

    # The checkpoint survives a server restart.
    with TestClient(create_app(tmp_path / 'workflow.sqlite3', seed_demo=True)) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        status = client.get(path).json()
        assert status['next_step'] == 'complete'
        assert status['last_step'] == 'human_review'

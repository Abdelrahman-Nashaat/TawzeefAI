"""Exercise the synthetic scheduling slice against PostgreSQL, including races."""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401

from app.main import create_app


def test_screened_hr_decision_and_concurrent_booking(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    with TestClient(app) as first, TestClient(app) as second, TestClient(app) as hr:
        for client, username in ((first, 'candidate'), (second, 'candidate2'), (hr, 'hr')):
            login(client, username)
        job = first.get('/api/jobs').json()[0]['id']
        ids = []
        for client in (first, second):
            response = client.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes())
            assert response.status_code == 201, response.text
            ids.append(response.json()['id'])
        assert hr.post(f'/api/applications/{ids[0]}/decision', json={'decision': 'advance'}).status_code == 409
        assert first.post(f'/api/applications/{ids[0]}/decision', json={'decision': 'advance'}).status_code == 403
        for app_id in ids:
            response = hr.post(f'/api/applications/{app_id}/screen')
            assert response.status_code == 200, response.text
        for app_id in ids:
            response = hr.post(f'/api/applications/{app_id}/decision', json={'decision': 'advance', 'note': 'Reviewed'})
            assert response.status_code == 200, response.text
        start = datetime.now(UTC) + timedelta(days=1)
        payload = {'start_at': start.isoformat(), 'end_at': (start + timedelta(hours=1)).isoformat(), 'interviewer': 'Example Interviewer'}
        response = hr.post('/api/slots', json=payload)
        assert response.status_code == 201, response.text
        slot = response.json()['id']
        assert hr.post('/api/slots', json=payload).status_code == 409
        assert first.get('/api/slots', params={'application_id': ids[1]}).status_code == 404
        assert len(first.get('/api/slots', params={'application_id': ids[0]}).json()) == 1
        barrier = threading.Barrier(2)
        def book(client, application_id):
            barrier.wait(timeout=10)
            return client.post(f'/api/slots/{slot}/book', json={'application_id': application_id}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(book, first, ids[0])
            b = pool.submit(book, second, ids[1])
            assert sorted([a.result(timeout=20), b.result(timeout=20)]) == [201, 409]
        with db_for(mode) as db:
            assert db.execute("SELECT count(*) AS n FROM interviews WHERE status='scheduled'").fetchone()['n'] == 1
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='decision'").fetchone()['n'] == 2
            assert db.execute("SELECT count(*) AS n FROM audit_events WHERE action='interview_scheduled'").fetchone()['n'] == 1

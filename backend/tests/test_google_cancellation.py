from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


def test_confirmed_or_uncertain_external_calendar_blocks_local_cancellation(tmp_path):
    path = tmp_path / 'google-cancel.sqlite3'
    app = create_app(path, seed_demo=True)
    app.state.google_dispatcher = lambda **kwargs: {'status': 'confirmed', 'provider_id': kwargs['event_id']}
    with TestClient(app) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text)
                VALUES (?, 'job-backend-python', 'synthetic.docx', 'synthetic')""", (candidate,)).lastrowid
        start = (datetime.now(UTC) + timedelta(days=3)).replace(microsecond=0)
        response = client.post('/api/interviews', json={'application_id': application, 'start_at': start.isoformat(),
            'end_at': (start + timedelta(hours=1)).isoformat(), 'interviewer': 'Synthetic HR'})
        assert response.status_code == 201
        interview = response.json()['id']
        draft = client.post(f'/api/google/interviews/{interview}/deliveries', json={'kind': 'calendar', 'confirmed': True})
        assert draft.status_code == 201
        delivery = draft.json()['id']
        sent = client.post(f'/api/google/deliveries/{delivery}/send', json={'confirmed': True})
        assert sent.status_code == 200 and sent.json()['state'] == 'confirmed'
        assert client.post(f'/api/interviews/{interview}/cancel').status_code == 409
        assert client.post(f'/api/applications/{application}/decision', json={'decision': 'reject'}).status_code == 409
        with connect(path) as db:
            assert db.execute('SELECT status FROM interviews WHERE id=?', (interview,)).fetchone()[0] == 'scheduled'
            assert db.execute('SELECT status FROM applications WHERE id=?', (application,)).fetchone()[0] == 'submitted'

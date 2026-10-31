from datetime import UTC, datetime, timedelta

import pytest

from app.db import connect
from app.scheduling import utc_text


@pytest.fixture
def accepted_offer():
    def prepare(client, application_id):
        now = datetime.now(UTC)
        with connect(client.app.state.database_path) as db:
            candidate = db.execute('SELECT candidate_id FROM applications WHERE id=?', (application_id,)).fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            interview = db.execute('''INSERT INTO interviews(application_id,candidate_id,start_at,end_at,interviewer,interviewer_key,created_by)
                VALUES (?,?,?,?,?,?,?)''', (application_id, candidate, utc_text(now - timedelta(hours=2)),
                                            utc_text(now - timedelta(hours=1)), 'HR', 'hr', hr)).lastrowid
            db.execute('INSERT INTO interview_completions(interview_id,recorded_by) VALUES (?,?)', (interview, hr))
        response = client.post(f'/api/applications/{application_id}/offers', json={'details': 'Synthetic offer'})
        assert response.status_code == 201, response.text
        offer_id = response.json()['id']
        assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
        assert client.post(f'/api/offers/{offer_id}/respond', json={'decision': 'accepted'}).status_code == 200
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
    return prepare

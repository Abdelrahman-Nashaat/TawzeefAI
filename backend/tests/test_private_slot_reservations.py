from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.auth import hash_password
from app.db import connect
from app.graph_orchestrator import _current_scheduling
from app.main import create_app
from app.scheduling import utc_text


def login(client, username, password):
    assert client.post('/api/auth/login', json={'username': username, 'password': password}).status_code == 200


def setup_apps(path):
    with connect(path) as db:
        first = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
        other = db.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES ('other','Other','candidate',?)", (hash_password('other-password'),)).lastrowid
        return [db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?, 'job-backend-python','cv.pdf','synthetic','advanced')", (owner,)).lastrowid for owner in (first, other)]


def test_private_reservation_blocks_conflicting_paths_and_withdrawal_releases(tmp_path):
    path = tmp_path / 'reservations.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        first, other = setup_apps(path)
        login(client, 'hr', 'hr-demo-only')
        start = datetime.now(UTC) + timedelta(days=3)
        slot = {'start_at': start.isoformat(), 'end_at': (start + timedelta(minutes=30)).isoformat(), 'interviewer': 'Mona'}
        preview = client.post('/api/slots/batches/preview', json={'items': [{**slot, 'application_id': first}]}).json()
        committed = client.post(f"/api/slots/batches/{preview['proposal_id']}/commit", json={'fingerprint': preview['fingerprint']})
        assert committed.status_code == 200
        private_id = committed.json()['slots'][0]['id']
        assert client.post('/api/slots', json=slot).status_code == 409
        assert client.post('/api/interviews', json={**slot, 'application_id': other}).status_code == 409
        # Simulate a legacy general slot that predated the reservation guard.
        with connect(path) as db:
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            general_id = db.execute('INSERT INTO interview_slots(start_at,end_at,interviewer,interviewer_key,created_by) VALUES (?,?,?,?,?)',
                                    (utc_text(start), utc_text(start + timedelta(minutes=30)), 'Mona', 'mona', hr)).lastrowid
        assert _current_scheduling(path, other)['available_slots'] == []
        login(client, 'other', 'other-password')
        assert client.get('/api/slots', params={'application_id': other}).json() == []
        assert client.post(f'/api/slots/{general_id}/book', json={'application_id': other}).status_code == 409
        login(client, 'candidate', 'candidate-demo-only')
        assert [s['id'] for s in client.get('/api/slots', params={'application_id': first}).json()] == [private_id]
        assert client.post(f'/api/slots/{private_id}/book', json={'application_id': first}).status_code == 201
        login(client, 'hr', 'hr-demo-only')
        interviews = client.get('/api/interviews').json()
        assert client.post(f"/api/interviews/{interviews[0]['id']}/cancel").status_code == 200
        assert client.post('/api/slots', json=slot).status_code == 409
        assert client.post(f'/api/slots/{private_id}/withdraw').status_code == 200
        assert client.post('/api/slots', json=slot).status_code == 201


def test_private_commit_and_overlapping_general_creation_are_serialized(tmp_path):
    path = tmp_path / 'race.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        first, _ = setup_apps(path)
        login(client, 'hr', 'hr-demo-only')
        start = datetime.now(UTC) + timedelta(days=3)
        slot = {'start_at': start.isoformat(), 'end_at': (start + timedelta(minutes=30)).isoformat(), 'interviewer': 'Mona'}
        preview = client.post('/api/slots/batches/preview', json={'items': [{**slot, 'application_id': first}]}).json()
        with ThreadPoolExecutor(max_workers=2) as pool:
            create = pool.submit(client.post, '/api/slots', json=slot)
            commit = pool.submit(client.post, f"/api/slots/batches/{preview['proposal_id']}/commit", json={'fingerprint': preview['fingerprint']})
            assert sorted((create.result().status_code, commit.result().status_code)) == [201, 409] or sorted((create.result().status_code, commit.result().status_code)) == [200, 409]
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 1


def test_graph_approval_rechecks_candidate_private_reservation(tmp_path):
    path = tmp_path / 'graph_reservation.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        first, _ = setup_apps(path)
        login(client, 'hr', 'hr-demo-only')
        start = datetime.now(UTC) + timedelta(days=3)
        common = {'start_at': start.isoformat(), 'end_at': (start + timedelta(minutes=30)).isoformat()}
        review = client.post('/api/graph-reviews', json={'application_id': first, 'intent': 'create_slot',
                             'slot': {**common, 'interviewer': 'Other interviewer'}})
        assert review.status_code == 201, review.text
        review = review.json()
        preview = client.post('/api/slots/batches/preview', json={'items': [{**common, 'application_id': first, 'interviewer': 'Mona'}]}).json()
        assert client.post(f"/api/slots/batches/{preview['proposal_id']}/commit", json={'fingerprint': preview['fingerprint']}).status_code == 200
        approval = {'reviewed': True, 'approval_fingerprint': review['state']['action_proposal']['approval_fingerprint']}
        assert client.post(f"/api/graph-reviews/{review['id']}/resume", json=approval).status_code == 409
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 1

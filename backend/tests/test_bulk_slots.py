import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import slots as slot_module
from app.auth import hash_password
from app.db import connect
from app.graph_orchestrator import _current_scheduling
from app.main import create_app
from app.slots import initialize_slots


def login(client, name, password):
    assert client.post('/api/auth/login', json={'username': name, 'password': password}).status_code == 200


def test_bulk_slots_are_private_to_each_application_and_commit_once(tmp_path):
    path = tmp_path / 'bulk.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            other = db.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES ('other','Other','candidate',?)", (hash_password('other-password'),)).lastrowid
            ids = [db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?, 'job-backend-python','cv.pdf','synthetic','advanced')", (owner,)).lastrowid for owner in (candidate, other)]
        login(client, 'hr', 'hr-demo-only')
        start = datetime.now(UTC) + timedelta(days=4)
        items = [{'application_id': app, 'start_at': (start + timedelta(hours=i)).isoformat(),
                  'end_at': (start + timedelta(hours=i, minutes=30)).isoformat(), 'interviewer': 'Mona'}
                 for i, app in enumerate(ids)]
        preview = client.post('/api/slots/batches/preview', json={'items': items})
        assert preview.status_code == 201, preview.text
        plan = preview.json()
        assert [item['candidate_id'] for item in plan['items']] == [candidate, other]
        endpoint = f"/api/slots/batches/{plan['proposal_id']}/commit"
        assert client.post(endpoint, json={'fingerprint': '0' * 64}).status_code == 409
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: client.post(endpoint, json={'fingerprint': plan['fingerprint']}), range(2)))
        assert sorted(reply.status_code for reply in replies) == [200, 409]
        slots = next(reply.json()['slots'] for reply in replies if reply.status_code == 200)
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 2
            assert db.execute('SELECT count(*) FROM slot_audit_events').fetchone()[0] == 2
            assert db.execute('SELECT count(*) FROM interviews').fetchone()[0] == 0
        login(client, 'candidate', 'candidate-demo-only')
        assert [row['id'] for row in client.get('/api/slots', params={'application_id': ids[0]}).json()] == [slots[0]['id']]
        assert client.post(f"/api/slots/{slots[1]['id']}/book", json={'application_id': ids[0]}).status_code == 404
        assert client.post(f"/api/slots/{slots[0]['id']}/book", json={'application_id': ids[0]}).status_code == 201
        login(client, 'other', 'other-password')
        assert [row['id'] for row in client.get('/api/slots', params={'application_id': ids[1]}).json()] == [slots[1]['id']]


def test_changed_application_owner_requires_new_review(tmp_path):
    path = tmp_path / 'owner.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            other = db.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES ('other','Other','candidate',?)", (hash_password('other-password'),)).lastrowid
            app = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?, 'job-backend-python','cv.pdf','synthetic','advanced')", (candidate,)).lastrowid
        login(client, 'hr', 'hr-demo-only')
        start = datetime.now(UTC) + timedelta(days=4)
        response = client.post('/api/slots/batches/preview', json={'items': [{'application_id': app, 'start_at': start.isoformat(), 'end_at': (start + timedelta(minutes=30)).isoformat(), 'interviewer': 'Mona'}]})
        assert response.status_code == 201
        plan = response.json()
        assert plan['items'][0]['candidate_id'] == candidate
        with connect(path) as db:
            db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (other, app))
        assert _current_scheduling(path, app)['available_slots'] == []
        assert client.post(f"/api/slots/batches/{plan['proposal_id']}/commit", json={'fingerprint': plan['fingerprint']}).status_code == 409
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM slot_audit_events').fetchone()[0] == 0


def test_private_slot_stays_bound_to_original_candidate_after_transfer(tmp_path):
    path = tmp_path / 'transfer.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        with connect(path) as db:
            original = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            other = db.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES ('other','Other','candidate',?)", (hash_password('other-password'),)).lastrowid
            app = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?, 'job-backend-python','cv.pdf','synthetic','advanced')", (original,)).lastrowid
        login(client, 'hr', 'hr-demo-only')
        start = datetime.now(UTC) + timedelta(days=4)
        proposal = client.post('/api/slots/batches/preview', json={'items': [{'application_id': app, 'start_at': start.isoformat(), 'end_at': (start + timedelta(minutes=30)).isoformat(), 'interviewer': 'Mona'}]}).json()
        slot = client.post(f"/api/slots/batches/{proposal['proposal_id']}/commit", json={'fingerprint': proposal['fingerprint']}).json()['slots'][0]
        with connect(path) as db:
            db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (other, app))
        assert _current_scheduling(path, app)['available_slots'] == []
        login(client, 'other', 'other-password')
        assert client.get('/api/slots', params={'application_id': app}).json() == []
        assert client.post(f"/api/slots/{slot['id']}/book", json={'application_id': app}).status_code == 404


def test_old_private_slot_without_candidate_binding_fails_closed(tmp_path):
    path = tmp_path / 'old-target.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            app = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?, 'job-backend-python','cv.pdf','synthetic','advanced')", (candidate,)).lastrowid
            start = datetime.now(UTC) + timedelta(days=4)
            slot = db.execute('''INSERT INTO interview_slots(start_at,end_at,interviewer,interviewer_key,created_by,target_application_id)
                VALUES (?,?,?,?,?,?)''', (start.isoformat().replace('+00:00', 'Z'), (start + timedelta(minutes=30)).isoformat().replace('+00:00', 'Z'), 'Mona', 'mona', hr, app)).lastrowid
        login(client, 'candidate', 'candidate-demo-only')
        assert client.get('/api/slots', params={'application_id': app}).json() == []
        assert client.post(f'/api/slots/{slot}/book', json={'application_id': app}).status_code == 404


def test_migration_preserves_unbound_private_and_public_slots(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE users(id INTEGER PRIMARY KEY);
            CREATE TABLE applications(id INTEGER PRIMARY KEY);
            CREATE TABLE interviews(id INTEGER PRIMARY KEY);
            CREATE TABLE interview_slots(id INTEGER PRIMARY KEY, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
                interviewer TEXT NOT NULL, interviewer_key TEXT NOT NULL, created_by INTEGER NOT NULL,
                interview_id INTEGER, withdrawn INTEGER NOT NULL DEFAULT 0, created_at TEXT,
                target_application_id INTEGER);
            INSERT INTO interview_slots VALUES(1,'2035-01-01T00:00:00Z','2035-01-01T01:00:00Z','A','a',1,NULL,0,'2034-01-01',1);
            INSERT INTO interview_slots VALUES(2,'2035-01-02T00:00:00Z','2035-01-02T01:00:00Z','B','b',1,NULL,0,'2034-01-01',NULL);''')
    initialize_slots(path)
    with connect(path) as db:
        rows = db.execute('SELECT id,target_application_id,target_candidate_id FROM interview_slots ORDER BY id').fetchall()
        assert [tuple(row) for row in rows] == [(1, 1, None), (2, None, None)]


def test_expired_session_before_commit_rolls_back_slots_and_audit(tmp_path, monkeypatch):
    path = tmp_path / 'expiry.sqlite3'
    with TestClient(create_app(path, seed_demo=True), raise_server_exceptions=False) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?, 'job-backend-python','cv.pdf','synthetic','advanced')", (candidate,)).lastrowid
        login(client, 'hr', 'hr-demo-only')
        start = datetime.now(UTC) + timedelta(days=4)
        plan = client.post('/api/slots/batches/preview', json={'items': [{'application_id': app, 'start_at': start.isoformat(), 'end_at': (start + timedelta(minutes=30)).isoformat(), 'interviewer': 'Mona'}]}).json()
        times = iter([time.time(), time.time() + 365 * 24 * 3600])
        with monkeypatch.context() as patch:
            patch.setattr(slot_module, 'time', SimpleNamespace(time=lambda: next(times)))
            response = client.post(f"/api/slots/batches/{plan['proposal_id']}/commit", json={'fingerprint': plan['fingerprint']})
            assert response.status_code == 401
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM slot_audit_events').fetchone()[0] == 0
            assert db.execute('SELECT status FROM slot_batches').fetchone()[0] == 'pending'
        assert client.post(f"/api/slots/batches/{plan['proposal_id']}/commit", json={'fingerprint': plan['fingerprint']}).status_code == 200


def test_audit_failure_rolls_back_entire_batch(tmp_path):
    path = tmp_path / 'audit.sqlite3'
    with TestClient(create_app(path, seed_demo=True), raise_server_exceptions=False) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?, 'job-backend-python','cv.pdf','synthetic','advanced')", (candidate,)).lastrowid
        login(client, 'hr', 'hr-demo-only')
        start = datetime.now(UTC) + timedelta(days=4)
        plan = client.post('/api/slots/batches/preview', json={'items': [{'application_id': app, 'start_at': start.isoformat(), 'end_at': (start + timedelta(minutes=30)).isoformat(), 'interviewer': 'Mona'}]}).json()
        with connect(path) as db:
            db.execute("CREATE TRIGGER fail_slot_audit BEFORE INSERT ON slot_audit_events BEGIN SELECT RAISE(ABORT, 'audit failure'); END")
        assert client.post(f"/api/slots/batches/{plan['proposal_id']}/commit", json={'fingerprint': plan['fingerprint']}).status_code == 500
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0
            assert db.execute('SELECT status FROM slot_batches').fetchone()[0] == 'pending'


def test_changed_application_aborts_entire_batch(tmp_path):
    path = tmp_path / 'changed.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            app = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?, 'job-backend-python','cv.pdf','synthetic','advanced')", (candidate,)).lastrowid
        login(client, 'hr', 'hr-demo-only')
        start = datetime.now(UTC) + timedelta(days=3)
        result = client.post('/api/slots/batches/preview', json={'items': [{'application_id': app, 'start_at': start.isoformat(), 'end_at': (start + timedelta(minutes=30)).isoformat(), 'interviewer': 'Mona'}]})
        assert result.status_code == 201
        with connect(path) as db:
            db.execute("UPDATE applications SET status='rejected' WHERE id=?", (app,))
        plan = result.json()
        assert client.post(f"/api/slots/batches/{plan['proposal_id']}/commit", json={'fingerprint': plan['fingerprint']}).status_code == 409
        with connect(path) as db:
            assert db.execute('SELECT count(*) FROM interview_slots').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM slot_audit_events').fetchone()[0] == 0

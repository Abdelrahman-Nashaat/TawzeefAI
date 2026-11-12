"""Reserved PostgreSQL slots are visible only to their bound applicant."""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace

from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401
from test_postgres_scheduling import ready_booking, wait_for_lock

from app.main import create_app


def test_private_slot_is_bound_to_application_and_released_on_rejection(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as first, TestClient(create_app(seed_demo=True)) as second, TestClient(create_app(seed_demo=True)) as hr:
        for client, username in ((first, 'candidate'), (second, 'candidate2'), (hr, 'hr')):
            login(client, username)
        job = first.get('/api/jobs').json()[0]['id']
        apps = []
        for client in (first, second):
            response = client.post('/api/applications', params={'job_id':job, 'filename':'cv.docx'}, content=resume_bytes())
            assert response.status_code == 201
            app = response.json()['id']; apps.append(app)
            assert hr.post(f'/api/applications/{app}/screen').status_code == 200
            assert hr.post(f'/api/applications/{app}/decision', json={'decision':'advance'}).status_code == 200
        start = datetime.now(UTC) + timedelta(days=2)
        payload = {'start_at':start.isoformat(), 'end_at':(start + timedelta(hours=1)).isoformat(), 'interviewer':'HR A'}
        endpoint = f'/api/applications/{apps[0]}/private-slots'
        assert first.post(endpoint, json=payload).status_code == 403
        created = hr.post(endpoint, json=payload)
        assert created.status_code == 201, created.text
        slot = created.json()['id']
        assert first.get(endpoint).status_code == 403
        assert [item['id'] for item in hr.get(endpoint).json()] == [slot]
        assert [item['id'] for item in first.get('/api/slots', params={'application_id':apps[0]}).json()] == [slot]
        assert second.get('/api/slots', params={'application_id':apps[1]}).json() == []
        assert second.post(f'/api/slots/{slot}/book', json={'application_id':apps[1]}).status_code == 404
        with db_for(mode) as db:
            original = db.execute('SELECT candidate_id FROM applications WHERE id=%s', (apps[0],)).fetchone()['candidate_id']
            replacement = db.execute("SELECT id FROM users WHERE username='candidate2'").fetchone()['id']
            db.execute('UPDATE applications SET candidate_id=%s WHERE id=%s', (replacement, apps[0]))
        assert first.get('/api/slots', params={'application_id':apps[0]}).status_code == 404
        assert second.get('/api/slots', params={'application_id':apps[0]}).json() == []
        assert second.post(f'/api/slots/{slot}/book', json={'application_id':apps[0]}).status_code == 404
        with db_for(mode) as db:
            db.execute('UPDATE applications SET candidate_id=%s WHERE id=%s', (original, apps[0]))
        assert hr.post(endpoint, json=payload).status_code == 409
        assert hr.post(f'/api/applications/{apps[0]}/decision', json={'decision':'reject', 'note':'Reviewed'}).status_code == 200
        assert hr.post('/api/slots', json=payload).status_code == 201
        with db_for(mode) as db:
            assert db.execute('SELECT withdrawn FROM interview_slots WHERE id=%s', (slot,)).fetchone()['withdrawn']
            assert db.execute("SELECT count(*) AS n FROM slot_audit_events WHERE slot_id=%s AND action='withdrawn_finalized'", (slot,)).fetchone()['n'] == 1


def test_private_slot_can_be_booked_and_cancelled_but_not_withdrawn_while_booked(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        login(candidate, 'candidate'); login(hr, 'hr')
        job = candidate.get('/api/jobs').json()[0]['id']
        app = candidate.post('/api/applications', params={'job_id':job, 'filename':'cv.docx'}, content=resume_bytes()).json()['id']
        assert hr.post(f'/api/applications/{app}/screen').status_code == 200
        assert hr.post(f'/api/applications/{app}/decision', json={'decision':'advance'}).status_code == 200
        start = datetime.now(UTC) + timedelta(days=3)
        payload = {'start_at':start.isoformat(), 'end_at':(start + timedelta(hours=1)).isoformat(), 'interviewer':'HR B'}
        slot = hr.post(f'/api/applications/{app}/private-slots', json=payload).json()['id']
        booking = candidate.post(f'/api/slots/{slot}/book', json={'application_id':app})
        assert booking.status_code == 201
        assert hr.post(f'/api/slots/{slot}/withdraw').status_code == 409
        assert candidate.post(f"/api/interviews/{booking.json()['id']}/candidate-cancel").status_code == 200
        assert hr.post(f'/api/slots/{slot}/withdraw').status_code == 200
        assert hr.get(f'/api/applications/{app}/private-slots').json() == []
        assert candidate.get('/api/slots', params={'application_id':app}).json() == []


def test_expired_hr_session_during_lock_wait_does_not_reserve(mode, monkeypatch):  # noqa: F811
    import app.postgres_experimental as pg_app

    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as hr:
        application, _ = ready_booking(candidate, hr)
        start = datetime.now(UTC) + timedelta(days=5)
        payload = {'start_at': start.isoformat(), 'end_at': (start + timedelta(hours=1)).isoformat(), 'interviewer': 'HR C'}
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), hashtext('tawzeef-scheduling-v1'))")
            future = pool.submit(hr.post, f'/api/applications/{application}/private-slots', json=payload)
            wait_for_lock(mode, 'advisory')
            monkeypatch.setattr(pg_app, 'time', SimpleNamespace(time=lambda: time.time() + 365 * 24 * 3600))
            blocker.commit()
            assert future.result(timeout=20).status_code == 401
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM interview_slots WHERE target_application_id=%s', (application,)).fetchone()['n'] == 0


def test_failed_finalization_audit_rolls_back_rejection_and_reservation(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True), raise_server_exceptions=False) as hr:
        application, _ = ready_booking(candidate, hr)
        start = datetime.now(UTC) + timedelta(days=8)
        payload = {'start_at': start.isoformat(), 'end_at': (start + timedelta(hours=1)).isoformat(), 'interviewer': 'HR D'}
        created = hr.post(f'/api/applications/{application}/private-slots', json=payload)
        assert created.status_code == 201, created.text
        slot = created.json()['id']
        with db_for(mode) as db:
            db.execute("ALTER TABLE slot_audit_events ADD CONSTRAINT reject_final_audit CHECK (action <> 'withdrawn_finalized')")
        response = hr.post(f'/api/applications/{application}/decision', json={'decision': 'reject', 'note': 'Reviewed'})
        assert response.status_code == 500
        with db_for(mode) as db:
            assert db.execute('SELECT status FROM applications WHERE id=%s', (application,)).fetchone()['status'] == 'advanced'
            assert not db.execute('SELECT withdrawn FROM interview_slots WHERE id=%s', (slot,)).fetchone()['withdrawn']


def test_concurrent_private_reservations_cannot_overlap_for_same_candidate(mode):  # noqa: F811
    with TestClient(create_app(seed_demo=True)) as candidate, TestClient(create_app(seed_demo=True)) as first, TestClient(create_app(seed_demo=True)) as second:
        application, _ = ready_booking(candidate, first)
        login(second, 'hr')
        start = datetime.now(UTC) + timedelta(days=9)
        gate = Barrier(2)
        def reserve(client, interviewer):
            gate.wait(timeout=10)
            return client.post(f'/api/applications/{application}/private-slots', json={
                'start_at': start.isoformat(), 'end_at': (start + timedelta(hours=1)).isoformat(), 'interviewer': interviewer,
            }).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(reserve, first, 'HR E'), pool.submit(reserve, second, 'HR F')]
            assert sorted(future.result(timeout=30) for future in results) == [201, 409]
        with db_for(mode) as db:
            assert db.execute('SELECT count(*) AS n FROM interview_slots WHERE target_application_id=%s AND NOT withdrawn', (application,)).fetchone()['n'] == 1

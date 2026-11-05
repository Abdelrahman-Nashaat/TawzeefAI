"""Candidate ownership and fresh availability in the PostgreSQL demo."""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode, resume_bytes  # noqa: F401

from app.main import create_app


def ready_booking(candidate, hr):
    login(candidate, 'candidate')
    login(hr, 'hr')
    job = candidate.get('/api/jobs').json()[0]['id']
    response = candidate.post('/api/applications', params={'job_id': job, 'filename': 'resume.docx'}, content=resume_bytes())
    assert response.status_code == 201, response.text
    application = response.json()['id']
    assert hr.post(f'/api/applications/{application}/screen').status_code == 200
    assert hr.post(f'/api/applications/{application}/decision', json={'decision': 'advance'}).status_code == 200
    start = datetime.now(UTC) + timedelta(days=1)
    response = hr.post('/api/slots', json={
        'start_at': start.isoformat(), 'end_at': (start + timedelta(hours=1)).isoformat(),
        'interviewer': 'Example',
    })
    assert response.status_code == 201, response.text
    return application, response.json()['id']


def wait_for_lock(mode):  # noqa: F811
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with db_for(mode) as db:
            waiting = db.execute('''SELECT 1 FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid
                WHERE NOT l.granted AND l.locktype='transactionid' AND a.datname=current_database() LIMIT 1''').fetchone()
        if waiting:
            return
        time.sleep(0.05)
    raise AssertionError('Expected waiting transaction lock')


def test_interview_reads_are_owner_scoped_and_survive_restart(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    with TestClient(app) as candidate, TestClient(app) as other, TestClient(app) as hr:
        assert candidate.get('/api/interviews').status_code == 401
        application, slot = ready_booking(candidate, hr)
        login(other, 'candidate2')
        booking = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application})
        assert booking.status_code == 201, booking.text
        expected = booking.json()
        assert candidate.get('/api/interviews').json() == [expected]
        assert other.get('/api/interviews').json() == []
        assert hr.get('/api/interviews').json() == [expected]
        with db_for(mode) as db:
            db.execute("UPDATE users SET role='employee' WHERE username='candidate2'")
        assert other.get('/api/interviews').status_code == 403

    with TestClient(create_app(seed_demo=True)) as candidate:
        login(candidate, 'candidate')
        assert candidate.get('/api/interviews').json() == [expected]


def test_availability_excludes_slot_expiring_during_lock_wait(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    with TestClient(app) as candidate, TestClient(app) as hr:
        application, _ = ready_booking(candidate, hr)
        start = datetime.now(UTC) + timedelta(seconds=6)
        slot = hr.post('/api/slots', json={
            'start_at': start.isoformat(),
            'end_at': (start + timedelta(minutes=30)).isoformat(),
            'interviewer': 'Short-lived slot',
        })
        assert slot.status_code == 201, slot.text
        slot_id = slot.json()['id']
        assert slot_id in [item['id'] for item in candidate.get('/api/slots', params={'application_id': application}).json()]
        with db_for(mode) as blocker, ThreadPoolExecutor(max_workers=1) as pool:
            blocker.execute('SELECT 1 FROM applications WHERE id=%s FOR UPDATE', (application,))
            future = pool.submit(candidate.get, '/api/slots', params={'application_id': application})
            try:
                wait_for_lock(mode)
                remaining = (start - datetime.now(UTC)).total_seconds()
                if remaining > 0:
                    time.sleep(remaining + 0.05)
            finally:
                blocker.commit()
            result = future.result(timeout=20)
        assert result.status_code == 200, result.text
        assert slot_id not in [item['id'] for item in result.json()]

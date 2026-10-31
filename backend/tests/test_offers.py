from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app
from app.scheduling import utc_text


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / 'offers.sqlite3'
    with TestClient(create_app(path, seed_demo=True)) as client:
        login(client, 'hr')
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            app_id = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','test.pdf','synthetic','advanced')", (candidate,)).lastrowid
        yield client, path, app_id, candidate, hr


def login(client, role):
    assert client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'}).status_code == 200


def completed_interview(path, app_id, candidate, hr):
    now = datetime.now(UTC)
    with connect(path) as db:
        interview = db.execute('''INSERT INTO interviews(application_id,candidate_id,start_at,end_at,interviewer,interviewer_key,created_by)
            VALUES (?,?,?,?,?,?,?)''', (app_id, candidate, utc_text(now - timedelta(hours=2)),
                                        utc_text(now - timedelta(hours=1)), 'HR', 'hr', hr)).lastrowid
        db.execute('INSERT INTO interview_completions(interview_id,recorded_by) VALUES (?,?)', (interview, hr))


def test_offer_acceptance_and_hire(setup):
    client, path, app_id, candidate, hr = setup
    endpoint = f'/api/applications/{app_id}/offers'
    assert client.post(endpoint, json={'details': 'Position and compensation'}).status_code == 409
    assert client.post(f'/api/applications/{app_id}/decision', json={'decision': 'hire'}).status_code == 409
    completed_interview(path, app_id, candidate, hr)
    offer = client.post(endpoint, json={'details': 'Position and compensation'})
    assert offer.status_code == 201
    offer_id = offer.json()['id']
    assert client.post(endpoint, json={'details': 'Duplicate'}).status_code == 409
    assert client.post(f'/api/applications/{app_id}/decision', json={'decision': 'hire'}).status_code == 409
    login(client, 'candidate')
    assert client.get(endpoint).json()[0]['details'] == 'Position and compensation'
    assert client.get(f'/api/workflows/{app_id}/progress').json()['stage'] == 'offered'
    assert client.post(f'/api/offers/{offer_id}/respond', json={'decision': 'accepted'}).status_code == 200
    assert client.post(f'/api/offers/{offer_id}/respond', json={'decision': 'declined'}).status_code == 409
    login(client, 'hr')
    assert client.post(f'/api/applications/{app_id}/decision', json={'decision': 'hire'}).status_code == 200
    assert client.get(f'/api/applications/{app_id}').json()['status'] == 'hired'
    events = client.get('/api/audit').json()
    assert {'offer_presented', 'offer_accepted'} <= {event['action'] for event in events}
    assert all('Position and compensation' not in event['details'] for event in events)


def test_active_interview_blocks_offer_and_hiring(setup):
    client, path, app_id, candidate, hr = setup
    completed_interview(path, app_id, candidate, hr)
    now = datetime.now(UTC)
    with connect(path) as db:
        db.execute('''INSERT INTO interviews(application_id,candidate_id,start_at,end_at,interviewer,interviewer_key,created_by)
            VALUES (?,?,?,?,?,?,?)''', (app_id, candidate, utc_text(now + timedelta(hours=1)),
                                        utc_text(now + timedelta(hours=2)), 'HR', 'hr', hr))
    assert client.post(f'/api/applications/{app_id}/offers', json={'details': 'Offer'}).status_code == 409


def test_decline_withdraw_and_reject(setup):
    client, path, app_id, candidate, hr = setup
    completed_interview(path, app_id, candidate, hr)
    endpoint = f'/api/applications/{app_id}/offers'
    first = client.post(endpoint, json={'details': 'First'}).json()['id']
    login(client, 'candidate')
    assert client.post(f'/api/offers/{first}/respond', json={'decision': 'declined'}).status_code == 200
    login(client, 'hr')
    second = client.post(endpoint, json={'details': 'Second'}).json()['id']
    assert client.post(f'/api/offers/{second}/withdraw').status_code == 200
    third = client.post(endpoint, json={'details': 'Third'}).json()['id']
    assert client.post(f'/api/applications/{app_id}/decision', json={'decision': 'reject'}).status_code == 200
    assert client.get(endpoint).json()[0]['status'] == 'withdrawn'
    login(client, 'candidate')
    assert client.post(f'/api/offers/{third}/respond', json={'decision': 'accepted'}).status_code == 409


def test_concurrent_duplicate_offers_and_authorization(setup):
    client, path, app_id, candidate, hr = setup
    completed_interview(path, app_id, candidate, hr)
    endpoint = f'/api/applications/{app_id}/offers'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post(endpoint, json={'details': 'Offer'}).status_code, range(2)))
    assert sorted(results) == [201, 409]
    offer = client.get(endpoint).json()[0]
    login(client, 'employee')
    assert client.get(endpoint).status_code == 403
    assert client.post(f"/api/offers/{offer['id']}/respond", json={'decision': 'accepted'}).status_code == 403


def test_offer_audit_failure_rolls_back_all_changes(setup):
    import sqlite3

    client, path, app_id, candidate, hr = setup
    completed_interview(path, app_id, candidate, hr)
    with connect(path) as db:
        db.execute("CREATE TRIGGER abort_offer BEFORE INSERT ON audit_events WHEN NEW.action='offer_presented' BEGIN SELECT RAISE(ABORT, 'failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        client.post(f'/api/applications/{app_id}/offers', json={'details': 'Offer'})
    with connect(path) as db:
        assert db.execute('SELECT status FROM applications WHERE id=?', (app_id,)).fetchone()[0] == 'advanced'
        assert db.execute('SELECT count(*) FROM job_offers').fetchone()[0] == 0


def test_offer_is_hidden_from_other_candidate(setup):
    from app.auth import hash_password

    client, path, app_id, candidate, hr = setup
    completed_interview(path, app_id, candidate, hr)
    offer_id = client.post(f'/api/applications/{app_id}/offers', json={'details': 'Private offer'}).json()['id']
    with connect(path) as db:
        db.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES ('other','Other','candidate',?)", (hash_password('other-password'),))
    assert client.post('/api/auth/login', json={'username': 'other', 'password': 'other-password'}).status_code == 200
    assert client.get(f'/api/applications/{app_id}/offers').status_code == 403
    assert client.post(f'/api/offers/{offer_id}/respond', json={'decision': 'accepted'}).status_code == 404


def test_acceptance_and_withdrawal_are_mutually_exclusive(setup):
    client, path, app_id, candidate, hr = setup
    completed_interview(path, app_id, candidate, hr)
    offer_id = client.post(f'/api/applications/{app_id}/offers', json={'details': 'Offer'}).json()['id']
    with TestClient(client.app) as candidate_client:
        login(candidate_client, 'candidate')
        with ThreadPoolExecutor(max_workers=2) as pool:
            accept = pool.submit(candidate_client.post, f'/api/offers/{offer_id}/respond', json={'decision': 'accepted'})
            withdraw = pool.submit(client.post, f'/api/offers/{offer_id}/withdraw')
            assert sorted([accept.result().status_code, withdraw.result().status_code]) == [200, 409]
    with connect(path) as db:
        status = db.execute('SELECT status FROM job_offers WHERE id=?', (offer_id,)).fetchone()[0]
        application = db.execute('SELECT status FROM applications WHERE id=?', (app_id,)).fetchone()[0]
        assert (status, application) in [('accepted', 'offered'), ('withdrawn', 'advanced')]


@pytest.mark.parametrize('operation', ['present', 'accept'])
def test_expired_session_before_offer_commit_rolls_back(setup, monkeypatch, operation):
    import time
    from types import SimpleNamespace

    from app import recruitment

    client, path, app_id, candidate, hr = setup
    completed_interview(path, app_id, candidate, hr)
    if operation == 'accept':
        response = client.post(f'/api/applications/{app_id}/offers', json={'details': 'Offer'})
        offer_id = response.json()['id']
        login(client, 'candidate')
    readings = iter([time.time(), time.time() + 60 * 60 * 24 * 365])
    monkeypatch.setattr(recruitment, 'time', SimpleNamespace(time=lambda: next(readings)))
    if operation == 'present':
        response = client.post(f'/api/applications/{app_id}/offers', json={'details': 'Offer'})
    else:
        response = client.post(f'/api/offers/{offer_id}/respond', json={'decision': 'accepted'})
    assert response.status_code == 401
    with connect(path) as db:
        application = db.execute('SELECT status FROM applications WHERE id=?', (app_id,)).fetchone()[0]
        offers = db.execute('SELECT status FROM job_offers WHERE application_id=?', (app_id,)).fetchall()
        assert application == ('advanced' if operation == 'present' else 'offered')
        assert [offer[0] for offer in offers] == ([] if operation == 'present' else ['presented'])
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='offer_accepted' AND application_id=?", (app_id,)).fetchone()[0] == 0

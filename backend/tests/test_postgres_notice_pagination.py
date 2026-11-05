"""Notice pagination and per-HR acknowledgement on real PostgreSQL."""
from fastapi.testclient import TestClient
from test_postgres_cancellation_lifecycle import book
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401
from test_postgres_scheduling import ready_booking

from app.main import create_app


def page_ids(client, endpoint):
    first = client.get(endpoint, params={'limit': 2})
    assert first.status_code == 200, first.text
    page = first.json()
    assert len(page['items']) == 2
    assert page['next_before'] == page['items'][-1]['id']
    second = client.get(endpoint, params={'limit': 2, 'before': page['next_before']})
    assert second.status_code == 200, second.text
    rest = second.json()
    assert len(rest['items']) == 1
    assert rest['next_before'] is None
    ids = [item['id'] for item in page['items'] + rest['items']]
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == 3
    return ids


def test_paginated_notices_and_per_hr_read_state_survive_restart(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    with TestClient(app) as candidate, TestClient(app) as hr, TestClient(app) as hr2:
        application, slot = ready_booking(candidate, hr)
        with db_for(mode) as db:
            db.execute('''INSERT INTO users(username,display_name,role,password_hash,active)
                SELECT 'hr2','Second HR','hr',password_hash,1 FROM users WHERE username='hr' ''')
        response = hr2.post('/api/auth/login', json={'username': 'hr2', 'password': 'hr-demo-only'})
        assert response.status_code == 200, response.text
        for _ in range(3):
            interview = book(candidate, application, slot)
            assert candidate.post(f'/api/interviews/{interview}/candidate-cancel').status_code == 200
        endpoint = '/api/hr/interview-cancellations'
        ids = page_ids(hr, endpoint)
        assert page_ids(hr2, endpoint) == ids
        assert hr.post(f'{endpoint}/{ids[0]}/read').status_code == 200
        assert [item['is_read'] for item in hr.get(endpoint).json()['items']] == [True, False, False]
        assert [item['is_read'] for item in hr2.get(endpoint).json()['items']] == [False, False, False]
        assert hr2.post(f'{endpoint}/{ids[1]}/read').status_code == 200
        assert [item['is_read'] for item in hr.get(endpoint).json()['items']] == [True, False, False]
        assert [item['is_read'] for item in hr2.get(endpoint).json()['items']] == [False, True, False]
        assert candidate.get(endpoint).status_code == 403
        assert candidate.post(f'{endpoint}/{ids[0]}/read').status_code == 403
        for _ in range(3):
            interview = book(candidate, application, slot)
            assert hr.post(f'/api/interviews/{interview}/cancel').status_code == 200
        candidate_endpoint = '/api/candidate/interview-cancellations'
        candidate_ids = page_ids(candidate, candidate_endpoint)
        assert candidate.post(f'{candidate_endpoint}/{candidate_ids[0]}/read').status_code == 200
        assert hr.get(candidate_endpoint).status_code == 403
    with TestClient(create_app(seed_demo=True)) as restarted:
        login(restarted, 'hr')
        assert [item['is_read'] for item in restarted.get(endpoint).json()['items']] == [True, False, False]
        login(restarted, 'candidate')
        assert [item['is_read'] for item in restarted.get(candidate_endpoint).json()['items']] == [True, False, False]

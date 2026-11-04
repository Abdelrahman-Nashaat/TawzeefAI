"""Use a synthetic provider to exercise changes while delivery is in flight."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_google_outbox import demo, login  # noqa: F401

from app.db import connect


@pytest.mark.parametrize('outcome', ['confirmed', 'rejected', 'uncertain'])
@pytest.mark.parametrize('change', ['disabled', 'role_restored', 'owner_restored'])
def test_context_change_during_delivery_is_recorded(demo, outcome, change):  # noqa: F811
    client, app, path, interview, hr = demo
    login(client, 'hr')
    address = 'fictional@example.test'
    response = client.post(f'/api/google/interviews/{interview}/deliveries', json={
        'kind': 'email', 'recipient': address, 'confirmed': True,
    })
    assert response.status_code == 201
    delivery_id = response.json()['id']
    entered, release = threading.Event(), threading.Event()

    def dispatcher(**_kwargs):
        entered.set()
        assert release.wait(10)
        return {'status': outcome, 'provider_id': 'synthetic-id'}

    app.state.google_dispatcher = dispatcher
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(client.post, f'/api/google/deliveries/{delivery_id}/send', json={
                'confirmed': True, 'expected_recipient': address,
            })
            assert entered.wait(10)
            with connect(path) as db:
                current = db.execute('SELECT candidate_id,application_id FROM interviews WHERE id=?', (interview,)).fetchone()
                if change == 'disabled':
                    db.execute('UPDATE users SET active=0 WHERE id=?', (current['candidate_id'],))
                elif change == 'role_restored':
                    db.execute("UPDATE users SET role='employee' WHERE id=?", (current['candidate_id'],))
                    db.execute("UPDATE users SET role='candidate' WHERE id=?", (current['candidate_id'],))
                else:
                    db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (hr, current['application_id']))
                    db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (current['candidate_id'], current['application_id']))
            release.set()
            result = future.result(timeout=10)
    finally:
        release.set()
    assert result.status_code == 200
    assert result.json()['state'] == outcome
    assert result.json()['context_changed_during_send'] is True
    listing = client.get(f'/api/google/interviews/{interview}/deliveries')
    assert listing.status_code == 200
    assert listing.json()[0]['context_changed_during_send'] is True
    with connect(path) as db:
        events = db.execute("SELECT details FROM audit_events WHERE action='google_delivery_context_changed_during_send'").fetchall()
        assert len(events) == 1
        assert address not in events[0]['details']


def test_delivery_without_context_change_has_no_warning(demo):  # noqa: F811
    client, app, path, interview, _ = demo
    login(client, 'hr')
    delivery = client.post(f'/api/google/interviews/{interview}/deliveries', json={
        'kind': 'email', 'recipient': 'fictional@example.test', 'confirmed': True,
    }).json()
    app.state.google_dispatcher = lambda **kwargs: {'status': 'confirmed', 'provider_id': 'synthetic-id'}
    result = client.post(f"/api/google/deliveries/{delivery['id']}/send", json={
        'confirmed': True, 'expected_recipient': 'fictional@example.test',
    })
    assert result.status_code == 200 and result.json()['context_changed_during_send'] is False
    with connect(path) as db:
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='google_delivery_context_changed_during_send'").fetchone()[0] == 0

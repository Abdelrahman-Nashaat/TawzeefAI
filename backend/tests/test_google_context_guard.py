import pytest
from test_google_outbox import demo, login  # noqa: F401

from app.db import connect


@pytest.mark.parametrize('change', ['owner', 'owner_reverted', 'candidate_disabled', 'role_reverted'])
@pytest.mark.parametrize('kind', ['email', 'calendar'])
def test_pending_stale_candidate_context_does_not_dispatch(demo, change, kind):  # noqa: F811
    client, app, path, interview, hr = demo
    login(client, 'hr')
    delivery = client.post(f'/api/google/interviews/{interview}/deliveries', json={
        'kind': kind, 'recipient': 'person@example.test' if kind == 'email' else None, 'confirmed': True,
    }).json()
    calls = []
    app.state.google_dispatcher = lambda **kw: calls.append(kw) or {'status': 'confirmed', 'provider_id': 'fake'}
    app.state.google_dispatcher.google_credential_binding = 'a' * 64
    with connect(path) as db:
        row = db.execute('SELECT application_id,candidate_id FROM interviews WHERE id=?', (interview,)).fetchone()
        if change.startswith('owner'):
            db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (hr, row['application_id']))
            if change == 'owner_reverted':
                db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (row['candidate_id'], row['application_id']))
        elif change == 'candidate_disabled':
            db.execute('UPDATE users SET active=0 WHERE id=?', (row['candidate_id'],))
        else:
            db.execute("UPDATE users SET role='employee' WHERE id=?", (row['candidate_id'],))
            db.execute("UPDATE users SET role='candidate' WHERE id=?", (row['candidate_id'],))
    response = client.post(f"/api/google/deliveries/{delivery['id']}/send", json={
        'confirmed': True, 'expected_recipient': 'person@example.test',
    })
    assert response.status_code == 409
    assert calls == []


def test_preview_change_restore_invalidates_preparation_and_stale_review_recovers(demo):  # noqa: F811
    client, app, path, interview, hr = demo
    login(client, 'hr')
    with connect(path) as db:
        db.execute("UPDATE applications SET status='advanced' WHERE id=(SELECT application_id FROM interviews WHERE id=?)", (interview,))
    body = {'recipient': 'person@example.test'}
    preview = client.post(f'/api/google/interviews/{interview}/delivery-proposal', json=body).json()
    with connect(path) as db:
        row = db.execute('SELECT application_id,candidate_id FROM interviews WHERE id=?', (interview,)).fetchone()
        db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (hr, row['application_id']))
        db.execute('UPDATE applications SET candidate_id=? WHERE id=?', (row['candidate_id'], row['application_id']))
    assert client.post(f'/api/google/interviews/{interview}/delivery-proposal/prepare', json={
        **body, 'fingerprint': preview['fingerprint'], 'confirmed': True,
    }).status_code == 409
    delivery = client.post(f'/api/google/interviews/{interview}/deliveries', json={
        'kind': 'email', **body, 'confirmed': True,
    }).json()
    with connect(path) as db:
        db.execute('UPDATE users SET active=0 WHERE id=?', (row['candidate_id'],))
        db.execute('UPDATE users SET active=1 WHERE id=?', (row['candidate_id'],))
    preview = client.post(f'/api/google/interviews/{interview}/delivery-proposal', json=body).json()
    assert preview['can_prepare'] is False
    action = f"/api/google/deliveries/{delivery['id']}"
    assert client.post(action + '/review-context', json={**body, 'fingerprint': preview['fingerprint'], 'confirmed': True}).json()['retry_generation'] == 1
    calls = []
    app.state.google_dispatcher = lambda **kwargs: calls.append(kwargs) or {'status': 'confirmed', 'provider_id': 'fake'}
    assert client.post(action + '/send', json={'confirmed': True, 'expected_recipient': body['recipient']}).status_code == 409
    assert calls == []
    assert client.post(action + '/send', json={'confirmed': True, 'expected_recipient': body['recipient'], 'expected_generation': 1}).status_code == 200
    assert len(calls) == 1


def test_candidate_change_during_preflight_never_claims_or_calls_provider(demo):  # noqa: F811
    client, app, path, interview, _ = demo
    login(client, 'hr')
    delivery = client.post(f'/api/google/interviews/{interview}/deliveries', json={
        'kind': 'email', 'recipient': 'person@example.test', 'confirmed': True,
    }).json()
    calls = []
    def preflight(kind):
        with connect(path) as db:
            candidate = db.execute('SELECT candidate_id FROM interviews WHERE id=?', (interview,)).fetchone()[0]
            db.execute('UPDATE users SET active=0 WHERE id=?', (candidate,))
            db.execute('UPDATE users SET active=1 WHERE id=?', (candidate,))
        return lambda **kwargs: calls.append(kwargs)
    app.state.google_preflight = preflight
    response = client.post(f"/api/google/deliveries/{delivery['id']}/send", json={
        'confirmed': True, 'expected_recipient': 'person@example.test',
    })
    assert response.status_code == 409 and calls == []
    with connect(path) as db:
        assert db.execute('SELECT state FROM google_deliveries WHERE id=?', (delivery['id'],)).fetchone()[0] == 'pending'


@pytest.mark.parametrize('state', ['pending', 'rejected'])
def test_legacy_draft_requires_explicit_proposal_review(demo, state):  # noqa: F811
    client, _, path, interview, _ = demo
    login(client, 'hr')
    with connect(path) as db:
        db.execute("UPDATE applications SET status='advanced' WHERE id=(SELECT application_id FROM interviews WHERE id=?)", (interview,))
    body = {'recipient': 'person@example.test'}
    delivery = client.post(f'/api/google/interviews/{interview}/deliveries', json={
        'kind': 'email', **body, 'confirmed': True,
    }).json()
    target = f"/api/google/deliveries/{delivery['id']}"
    with connect(path) as db:
        db.execute('DELETE FROM google_delivery_context WHERE delivery_id=?', (delivery['id'],))
        db.execute('UPDATE google_deliveries SET state=? WHERE id=?', (state, delivery['id']))
    preview = client.post(f'/api/google/interviews/{interview}/delivery-proposal', json=body).json()
    email = next(draft for draft in preview['drafts'] if draft['kind'] == 'email')
    assert not preview['can_prepare'] and email['context_current'] is False
    reviewed = client.post(target + '/review-context', json={**body, 'fingerprint': preview['fingerprint'], 'confirmed': True})
    assert reviewed.status_code == 200 and reviewed.json()['retry_generation'] == 1
    if state == 'rejected':
        assert client.post(target + '/reprepare', json={'confirmed': True, 'expected_generation': 1}).json()['retry_generation'] == 2

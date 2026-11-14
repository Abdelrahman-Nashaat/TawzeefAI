"""Only reviewed pending or definitely rejected Gmail drafts can be revised."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from test_postgres_experimental_api import db_for, login, mode  # noqa: F401
from test_postgres_scheduling import ready_booking

from app.main import create_app


def setup(candidate, hr):
    application, slot = ready_booking(candidate, hr)
    interview = candidate.post(f'/api/slots/{slot}/book', json={'application_id': application}).json()['id']
    url = f'/api/google/interviews/{interview}/email'
    draft = hr.post(url, json={'confirmed': True, 'recipient': 'first@example.com'}).json()
    return url, draft


def revision(draft, recipient='second@example.com'):
    return {'confirmed': True, 'delivery_id': draft['id'], 'expected_generation': draft['generation'], 'recipient': recipient}


def test_pending_correction_rejects_stale_send_and_persists_history(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    calls = []
    app.state.google_email_preflight = lambda _: lambda **kw: calls.append(kw) or {'status': 'confirmed', 'provider_id': 'fake'}
    with TestClient(app) as candidate, TestClient(app) as hr:
        url, draft = setup(candidate, hr)
        assert candidate.post(url + '/revise', json=revision(draft)).status_code == 403
        assert hr.post(url + '/revise', json={**revision(draft), 'confirmed': False}).status_code == 422
        updated = hr.post(url + '/revise', json=revision(draft)).json()
        assert updated['generation'] == 1 and updated['recipient'] == 'second@example.com'
        assert hr.post(url + '/revise', json=revision(draft)).status_code == 409
        stale = {'confirmed': True, 'delivery_id': draft['id'], 'recipient': 'second@example.com'}
        assert hr.post(url + '/send', json=stale).status_code == 409
        assert hr.post(url + '/send', json={**stale, 'generation': 0}).status_code == 409
        assert hr.post(url + '/send', json={**stale, 'generation': 1}).json()['state'] == 'confirmed'
        assert [call['recipient'] for call in calls] == ['second@example.com']
        assert hr.post(url + '/revise', json=revision(updated)).status_code == 409
        with db_for(mode) as db:
            prior = db.execute('SELECT generation,state,recipient FROM pg_email_revisions').fetchall()
            assert [(r['generation'], r['state'], r['recipient']) for r in prior] == [(0, 'pending', 'first@example.com')]
    with TestClient(create_app(seed_demo=True)) as hr:
        login(hr, 'hr')
        assert hr.get(url).json()['generation'] == 1


@pytest.mark.parametrize('outcome,allowed', [('rejected', True), ('uncertain', False), ('confirmed', False)])
def test_only_definite_rejection_can_be_reprepared(mode, outcome, allowed):  # noqa: F811
    app = create_app(seed_demo=True)
    app.state.google_email_preflight = lambda _: lambda **kw: {'status': outcome, 'provider_id': 'fake'}
    with TestClient(app) as candidate, TestClient(app) as hr:
        url, draft = setup(candidate, hr)
        assert hr.post(url + '/send', json={'confirmed': True, 'delivery_id': draft['id'],
                                           'recipient': draft['recipient'], 'generation': 0}).json()['state'] == outcome
        result = hr.post(url + '/revise', json=revision(draft))
        assert result.status_code == (200 if allowed else 409)
        if allowed:
            assert result.json()['state'] == 'pending' and result.json()['generation'] == 1
            with db_for(mode) as db:
                assert db.execute('SELECT state FROM pg_email_revisions').fetchone()['state'] == 'rejected'


def test_revision_during_preflight_invalidates_stale_send(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def preflight(_):
        entered.set()
        assert release.wait(30)
        return lambda **kw: calls.append(kw)
    app.state.google_email_preflight = preflight
    with TestClient(app) as candidate, TestClient(app) as hr, ThreadPoolExecutor(max_workers=1) as pool:
        url, draft = setup(candidate, hr)
        old = {'confirmed': True, 'delivery_id': draft['id'], 'recipient': draft['recipient'], 'generation': 0}
        pending = pool.submit(hr.post, url + '/send', json=old)
        assert entered.wait(30)
        try:
            assert hr.post(url + '/revise', json=revision(draft, recipient=draft['recipient'])).status_code == 200
        finally:
            release.set()
        assert pending.result(timeout=30).status_code == 409
        assert not calls


def test_revision_cannot_overtake_claimed_send(mode):  # noqa: F811
    app = create_app(seed_demo=True)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def dispatch(**kw):
        calls.append(kw)
        entered.set()
        assert release.wait(30)
        return {'status': 'confirmed', 'provider_id': 'fake'}
    app.state.google_email_preflight = lambda _: dispatch
    with TestClient(app) as candidate, TestClient(app) as hr, ThreadPoolExecutor(max_workers=1) as pool:
        url, draft = setup(candidate, hr)
        send = {'confirmed': True, 'delivery_id': draft['id'], 'recipient': draft['recipient'], 'generation': 0}
        pending = pool.submit(hr.post, url + '/send', json=send)
        assert entered.wait(30)
        try:
            assert hr.post(url + '/revise', json=revision(draft)).status_code == 409
        finally:
            release.set()
        assert pending.result(timeout=30).json()['state'] == 'confirmed'
        assert len(calls) == 1 and calls[0]['recipient'] == draft['recipient']

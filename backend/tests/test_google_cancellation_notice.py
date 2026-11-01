"""Cancellation notices use synthetic recipients and fake dispatch only."""

import base64
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / 'cancellation.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app) as client:
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
            application = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text,status) VALUES (?,'job-backend-python','synthetic.pdf','synthetic','advanced')", (candidate,)).lastrowid
            start = (datetime.now(UTC) + timedelta(days=2)).isoformat()
            end = (datetime.now(UTC) + timedelta(days=2, hours=1)).isoformat()
            interview = db.execute("""INSERT INTO interviews(application_id,candidate_id,start_at,end_at,
                interviewer,interviewer_key,created_by,status) VALUES (?,?,?,?,?,?,?,'cancelled')""",
                (application, candidate, start, end, 'Interviewer', 'interviewer', hr)).lastrowid
            confirmation = db.execute("""INSERT INTO google_deliveries(interview_id,application_id,kind,recipient,
                start_at,end_at,state,provider_id,created_by) VALUES (?,?,'email','candidate@example.test',?,?,'confirmed','original-message',?)""",
                (interview, application, start, end, hr)).lastrowid
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        yield client, app, path, interview, confirmation, hr


def preview(client, interview):
    response = client.get(f'/api/google/interviews/{interview}/cancellation-notice')
    assert response.status_code == 200, response.text
    return response.json()


def send(client, interview, fingerprint):
    return client.post(f'/api/google/interviews/{interview}/cancellation-notice/send',
                       json={'confirmed': True, 'fingerprint': fingerprint})


def test_review_then_one_explicit_send_with_original_recipient(setup):
    client, app, path, interview, _, _ = setup
    calls = []
    app.state.google_preflight = lambda kind: lambda **kwargs: calls.append((kind, kwargs)) or {
        'status': 'confirmed', 'provider_id': 'synthetic-message'}
    data = preview(client, interview)
    assert data['recipient'] == 'candidate@example.test' and data['can_send']
    assert 'does not verify the original sender' in data['notice']
    assert data['subject'] == 'Interview cancellation' and 'has been cancelled' in data['body']
    assert not calls
    assert send(client, interview, '0' * 64).status_code == 409
    response = send(client, interview, data['fingerprint'])
    assert response.status_code == 200 and response.json()['state'] == 'confirmed'
    assert len(calls) == 1 and calls[0][0] == 'cancellation_email'
    assert calls[0][1]['recipient'] == 'candidate@example.test'
    assert send(client, interview, data['fingerprint']).status_code == 409
    assert not preview(client, interview)['can_send']
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM cancellation_notices').fetchone()[0] == 1
        actions = [row[0] for row in db.execute("SELECT action FROM audit_events WHERE action LIKE 'cancellation_notice_%' ORDER BY id")]
        assert actions == ['cancellation_notice_started', 'cancellation_notice_result']


def test_uncertain_requires_external_attestation_and_new_review(setup):
    client, app, _, interview, _, _ = setup
    calls = []
    app.state.google_preflight = lambda _: lambda **kwargs: calls.append(kwargs) or {'status': 'uncertain'}
    first = preview(client, interview)
    sent = send(client, interview, first['fingerprint']).json()
    assert sent['state'] == 'uncertain' and len(calls) == 1
    blocked = preview(client, interview)
    assert blocked['last_notice']['id'] == sent['id'] and not blocked['can_send']
    assert send(client, interview, blocked['fingerprint']).status_code == 409
    resolve = f"/api/google/cancellation-notices/{sent['id']}/resolve"
    assert client.post(resolve, json={'confirmed': False, 'outcome': 'not_sent'}).status_code == 422
    assert client.post(resolve, json={'confirmed': True, 'outcome': 'not_sent'}).json()['state'] == 'verified_not_sent'
    assert client.post(resolve, json={'confirmed': True, 'outcome': 'not_sent'}).status_code == 409
    assert send(client, interview, first['fingerprint']).status_code == 409
    current = preview(client, interview)
    assert current['can_send'] and send(client, interview, current['fingerprint']).json()['state'] == 'uncertain'
    assert len(calls) == 2


def test_roles_and_no_original_confirmation_or_cancellation(setup):
    client, _, path, interview, confirmation, _ = setup
    token = preview(client, interview)['fingerprint']
    assert client.post('/api/auth/login', json={'username': 'candidate', 'password': 'candidate-demo-only'}).status_code == 200
    assert client.get(f'/api/google/interviews/{interview}/cancellation-notice').status_code == 403
    assert send(client, interview, token).status_code == 403
    assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
    with connect(path) as db:
        db.execute("UPDATE google_deliveries SET state='uncertain' WHERE id=?", (confirmation,))
    assert client.get(f'/api/google/interviews/{interview}/cancellation-notice').status_code == 409
    with connect(path) as db:
        db.execute("UPDATE google_deliveries SET state='confirmed' WHERE id=?", (confirmation,))
        db.execute("UPDATE interviews SET status='scheduled' WHERE id=?", (interview,))
    assert client.get(f'/api/google/interviews/{interview}/cancellation-notice').status_code == 409


def test_preflight_mutation_and_revocation_prevent_send(setup):
    client, app, path, interview, _, hr = setup
    data = preview(client, interview)
    calls = []
    def preflight(_):
        with connect(path) as db:
            db.execute("UPDATE interviews SET status='scheduled' WHERE id=?", (interview,))
        return lambda **kwargs: calls.append(kwargs)
    app.state.google_preflight = preflight
    assert send(client, interview, data['fingerprint']).status_code == 409 and not calls
    with connect(path) as db:
        db.execute("UPDATE interviews SET status='cancelled' WHERE id=?", (interview,))
    data = preview(client, interview)
    def revoke(_):
        with connect(path) as db:
            db.execute('UPDATE users SET active=0 WHERE id=?', (hr,))
        return lambda **kwargs: calls.append(kwargs)
    app.state.google_preflight = revoke
    assert send(client, interview, data['fingerprint']).status_code == 401 and not calls
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM cancellation_notices').fetchone()[0] == 0


def test_revocation_during_dispatch_records_result_but_hides_it(setup):
    client, app, path, interview, _, hr = setup
    def dispatch(**_):
        with connect(path) as db:
            db.execute('UPDATE users SET active=0 WHERE id=?', (hr,))
        return {'status': 'confirmed', 'provider_id': 'synthetic-message'}
    app.state.google_preflight = lambda _: dispatch
    data = preview(client, interview)
    response = send(client, interview, data['fingerprint'])
    assert response.status_code == 401 and 'candidate@example.test' not in response.text
    with connect(path) as db:
        assert db.execute('SELECT state FROM cancellation_notices').fetchone()[0] == 'confirmed'


def test_authorization_expiry_after_claim_records_no_send(setup, monkeypatch):
    from app import google_cancellation

    client, app, path, interview, _, _ = setup
    calls = []
    app.state.google_preflight = lambda _: lambda **kwargs: calls.append(kwargs)
    data = preview(client, interview)
    original = google_cancellation._actor
    checks = 0

    def expire(*args):
        nonlocal checks
        checks += 1
        if checks == 3:
            raise HTTPException(status_code=401, detail='Authentication required')
        return original(*args)

    monkeypatch.setattr(google_cancellation, '_actor', expire)
    assert send(client, interview, data['fingerprint']).status_code == 401
    assert calls == []
    with connect(path) as db:
        assert db.execute('SELECT state FROM cancellation_notices').fetchone()[0] == 'rejected'


def test_verified_sent_blocks_new_attempt_and_interrupted_send_requires_attestation(setup):
    client, app, path, interview, _, _ = setup
    app.state.google_preflight = lambda _: lambda **kwargs: {'status': 'uncertain'}
    sent = send(client, interview, preview(client, interview)['fingerprint']).json()
    resolve = f"/api/google/cancellation-notices/{sent['id']}/resolve"
    assert client.post(resolve, json={'confirmed': True, 'outcome': 'sent'}).json()['state'] == 'verified_sent'
    assert not preview(client, interview)['can_send']
    with connect(path) as db:
        db.execute("UPDATE cancellation_notices SET state='sending' WHERE id=?", (sent['id'],))
    assert not preview(client, interview)['can_send']
    assert client.post(resolve, json={'confirmed': True, 'outcome': 'not_sent'}).json()['state'] == 'verified_not_sent'
    assert preview(client, interview)['can_send']


def test_active_dispatch_cannot_be_reconciled_or_retried(setup):
    client, app, path, interview, _, _ = setup
    attempts = []

    def dispatch(**_):
        with connect(path) as db:
            notice_id = db.execute('SELECT id FROM cancellation_notices').fetchone()[0]
        attempts.append(client.post(f'/api/google/cancellation-notices/{notice_id}/resolve',
                                    json={'confirmed': True, 'outcome': 'not_sent'}).status_code)
        return {'status': 'confirmed', 'provider_id': 'synthetic-message'}

    app.state.google_preflight = lambda _: dispatch
    assert send(client, interview, preview(client, interview)['fingerprint']).json()['state'] == 'confirmed'
    assert attempts == [409]


def test_abandoned_process_releases_lock_but_requires_external_review(setup):
    client, app, path, interview, _, _ = setup
    app.state.google_preflight = lambda _: lambda **kwargs: {'status': 'uncertain'}
    notice = send(client, interview, preview(client, interview)['fingerprint']).json()
    with connect(path) as db:
        db.execute("UPDATE cancellation_notices SET state='sending' WHERE id=?", (notice['id'],))
    script = ("import sys,time; from app.google_cancellation import _notice_lock; "
              "lock=_notice_lock(sys.argv[1]); lock.__enter__(); print('locked',flush=True); time.sleep(120)")
    child = subprocess.Popen([sys.executable, '-c', script, str(path)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        resolve = f"/api/google/cancellation-notices/{notice['id']}/resolve"
        assert client.post(resolve, json={'confirmed': True, 'outcome': 'not_sent'}).status_code == 409
        child.terminate()
        child.wait(timeout=15)
        assert not preview(client, interview)['can_send']
        assert client.post(resolve, json={'confirmed': True, 'outcome': 'not_sent'}).json()['state'] == 'verified_not_sent'
        assert preview(client, interview)['can_send']
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=15)
        child.stdout.close()


@pytest.mark.parametrize('changed_field', ['start_at', 'end_at', 'application_id', 'recipient'])
def test_original_confirmation_changed_after_claim_prevents_provider_call(setup, monkeypatch, changed_field):
    from app import google_cancellation

    client, app, path, interview, confirmation, _ = setup
    calls = []
    app.state.google_preflight = lambda _: lambda **kwargs: calls.append(kwargs)
    data = preview(client, interview)
    original_actor = google_cancellation._actor
    checks = 0

    def mutate(*args):
        nonlocal checks
        checks += 1
        if checks == 3:
            with connect(path) as db:
                if changed_field == 'application_id':
                    candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()[0]
                    other = db.execute("INSERT INTO applications(candidate_id,job_id,filename,resume_text) VALUES (?,'job-backend-python','other.pdf','synthetic')", (candidate,)).lastrowid
                    db.execute('UPDATE google_deliveries SET application_id=? WHERE id=?', (other, confirmation))
                else:
                    value = 'other@example.test' if changed_field == 'recipient' else '2026-01-01T00:00:00Z'
                    db.execute(f'UPDATE google_deliveries SET {changed_field}=? WHERE id=?', (value, confirmation))
        return original_actor(*args)

    monkeypatch.setattr(google_cancellation, '_actor', mutate)
    assert send(client, interview, data['fingerprint']).status_code == 409
    assert calls == []
    with connect(path) as db:
        assert db.execute('SELECT state FROM cancellation_notices').fetchone()[0] == 'rejected'


def test_gmail_adapter_uses_exact_reviewed_message_without_real_network():
    from app.google_services import send_cancellation
    from app.google_templates import cancellation_content

    start, end = '2026-12-15T08:00:00Z', '2026-12-15T08:30:00Z'
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {'id': 'synthetic-provider-id'}

    class Session:
        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return Response()

    result = send_cancellation(Session(), recipient='candidate@example.test', start=start, end=end)
    assert result == {'status': 'confirmed', 'provider_id': 'synthetic-provider-id'}
    raw = calls[0][2]['json']['raw']
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
    subject, body = cancellation_content(start, end)
    assert message['To'] == 'candidate@example.test' and message['Subject'] == subject
    assert message.get_body(preferencelist=('plain',)).get_content().replace('\r\n', '\n') == body

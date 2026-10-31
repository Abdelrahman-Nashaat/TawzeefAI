import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import google_outbox
from app.db import connect
from app.main import create_app
from app.scheduling import utc_text


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / 'proposal.sqlite3'
    app = create_app(path, seed_demo=True)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
        with connect(path) as db:
            candidate = db.execute("SELECT id FROM users WHERE username='candidate'").fetchone()['id']
            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()['id']
            application = db.execute("""INSERT INTO applications(candidate_id,job_id,filename,resume_text,status)
                VALUES (?, 'job-backend-python', 'synthetic.docx', 'synthetic', 'advanced')""", (candidate,)).lastrowid
            start = datetime.now(UTC) + timedelta(days=3)
            interview = db.execute('''INSERT INTO interviews(application_id,candidate_id,start_at,end_at,
                interviewer,interviewer_key,created_by) VALUES (?,?,?,?,?,?,?)''',
                (application, candidate, utc_text(start), utc_text(start + timedelta(hours=1)),
                 'Synthetic', 'synthetic', hr)).lastrowid
        app.state.google_dispatcher = lambda **kwargs: pytest.fail('Proposal must never contact Google')
        yield client, path, interview


def preview(client, interview, recipient='candidate@example.test'):
    return client.post(f'/api/google/interviews/{interview}/delivery-proposal', json={'recipient': recipient})


def prepare(client, interview, proposal, recipient='candidate@example.test'):
    return client.post(f'/api/google/interviews/{interview}/delivery-proposal/prepare',
                       json={'recipient': recipient, 'fingerprint': proposal['fingerprint'], 'confirmed': True})


def test_proposal_prepares_two_reviewed_local_drafts_without_network(setup):
    client, path, interview = setup
    response = preview(client, interview)
    assert response.status_code == 200
    proposal = response.json()
    assert proposal['recipient_verified'] is False and proposal['can_prepare'] is True
    assert proposal['calendar'] == {'summary': 'TawzeefAI interview', 'visibility': 'private', 'attendees': []}
    assert proposal['email']['subject'] == 'Interview confirmation'
    assert proposal['email']['body'].startswith('Your interview is scheduled from ')
    assert [draft['state'] for draft in proposal['drafts']] == ['not_prepared', 'not_prepared']
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM google_deliveries').fetchone()[0] == 0
    result = prepare(client, interview, proposal)
    assert result.status_code == 200
    assert {(row['kind'], row['state']) for row in result.json()['drafts']} == {('calendar', 'pending'), ('email', 'pending')}
    assert prepare(client, interview, proposal).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM google_deliveries').fetchone()[0] == 2
        audits = db.execute("SELECT details FROM audit_events WHERE action='google_delivery_prepared'").fetchall()
        assert len(audits) == 2 and all('candidate@example.test' not in row['details'] for row in audits)


def test_stale_proposal_or_changed_recipient_cannot_create_drafts(setup):
    client, path, interview = setup
    proposal = preview(client, interview).json()
    assert prepare(client, interview, proposal, recipient='other@example.test').status_code == 409
    with connect(path) as db:
        db.execute("UPDATE interviews SET status='cancelled' WHERE id=?", (interview,))
    assert prepare(client, interview, proposal).status_code == 409
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM google_deliveries').fetchone()[0] == 0


def test_existing_draft_is_reused_only_if_unchanged_and_pending(setup):
    client, path, interview = setup
    existing = client.post(f'/api/google/interviews/{interview}/deliveries',
                           json={'kind': 'calendar', 'confirmed': True}).json()
    proposal = preview(client, interview).json()
    assert proposal['drafts'][0]['id'] == existing['id']
    assert prepare(client, interview, proposal).status_code == 200
    with connect(path) as db:
        db.execute("UPDATE google_deliveries SET state='uncertain' WHERE id=?", (existing['id'],))
    blocked = preview(client, interview).json()
    assert blocked['can_prepare'] is False
    assert prepare(client, interview, blocked).status_code == 409


def test_two_simultaneous_approvals_create_one_pair(setup):
    client, path, interview = setup
    proposal = preview(client, interview).json()
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: prepare(client, interview, proposal).status_code, range(2)))
    assert sorted(statuses) == [200, 409]
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM google_deliveries').fetchone()[0] == 2


def test_candidate_and_employee_cannot_preview_or_prepare(setup):
    client, _, interview = setup
    proposal = preview(client, interview).json()
    for role in ('candidate', 'employee'):
        assert client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'}).status_code == 200
        assert preview(client, interview).status_code == 403
        assert prepare(client, interview, proposal).status_code == 403


def test_failure_after_first_insert_rolls_back_both_drafts(setup):
    client, path, interview = setup
    proposal = preview(client, interview).json()
    with connect(path) as db:
        db.execute("""CREATE TRIGGER fail_email BEFORE INSERT ON google_deliveries
                      WHEN NEW.kind='email' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
    assert prepare(client, interview, proposal).status_code == 500
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM google_deliveries').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='google_delivery_prepared'").fetchone()[0] == 0


def test_session_expiring_before_commit_rolls_back_both_drafts(setup, monkeypatch):
    client, path, interview = setup
    proposal = preview(client, interview).json()
    current = time.time()
    checks = iter([current, current + 365 * 24 * 60 * 60])
    monkeypatch.setattr(google_outbox, 'time', SimpleNamespace(time=lambda: next(checks)))
    assert prepare(client, interview, proposal).status_code == 401
    with connect(path) as db:
        assert db.execute('SELECT count(*) FROM google_deliveries').fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM audit_events WHERE action='google_delivery_prepared'").fetchone()[0] == 0


def test_reviewed_content_matches_provider_request_without_external_network(setup):
    import base64
    from email import policy
    from email.parser import BytesParser

    from app.google_services import create_calendar_event, send_confirmation

    class Response:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class SyntheticSession:
        def __init__(self, payload):
            self.payload = payload
            self.request_args = None

        def request(self, method, url, **kwargs):
            self.request_args = kwargs
            return Response(self.payload)

    client, _, interview = setup
    proposal = preview(client, interview).json()
    start, end = proposal['start_at'], proposal['end_at']
    calendar = SyntheticSession({'id': 'abc123', 'etag': '"v1"'})
    create_calendar_event(calendar, event_id='abc123', start=start, end=end)
    event = calendar.request_args['json']
    assert event['summary'] == proposal['calendar']['summary']
    assert event['visibility'] == proposal['calendar']['visibility']
    assert event.get('attendees', []) == proposal['calendar']['attendees']

    mail = SyntheticSession({'id': 'synthetic-message'})
    send_confirmation(mail, recipient=proposal['recipient'], start=start, end=end)
    raw = mail.request_args['json']['raw']
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
    assert message['To'] == proposal['recipient']
    assert message['Subject'] == proposal['email']['subject']
    # SMTP uses CRLF on the wire; compare the actual text after newline normalization.
    assert message.get_body(preferencelist=('plain',)).get_content().replace('\r\n', '\n') == proposal['email']['body']

import base64
import importlib
import importlib.util
from email import policy
from email.parser import BytesParser

import pytest


class Response:
    def __init__(self, status, data):
        self.status_code = status
        self.data = data

    def json(self):
        return self.data


class Session:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def provider():
    assert importlib.util.find_spec('app.google_services') is not None
    return importlib.import_module('app.google_services')


def test_calendar_creates_private_event_without_guests_or_notifications():
    module = provider()
    session = Session(Response(200, {'id': 'abc123'}))
    result = module.create_calendar_event(session, event_id='abc123',
        start='2026-11-01T09:00:00Z', end='2026-11-01T10:00:00Z')
    assert result == {'status': 'confirmed', 'provider_id': 'abc123'}
    method, url, request = session.calls[0]
    assert method == 'POST' and url == 'https://www.googleapis.com/calendar/v3/calendars/primary/events'
    assert request['params'] == {'sendUpdates': 'none'} and request['allow_redirects'] is False
    assert request['json']['id'] == 'abc123' and 'attendees' not in request['json']
    assert request['json']['start']['dateTime'] == '2026-11-01T09:00:00Z'


def test_gmail_builds_one_explicit_recipient_and_protects_headers():
    module = provider()
    session = Session(Response(200, {'id': 'message-1'}))
    result = module.send_confirmation(session, recipient='candidate@example.test',
        start='2026-11-01T09:00:00Z', end='2026-11-01T10:00:00Z')
    assert result == {'status': 'confirmed', 'provider_id': 'message-1'}
    method, url, request = session.calls[0]
    assert method == 'POST' and url == 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send'
    assert request['allow_redirects'] is False
    raw = request['json']['raw']
    message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
    assert message['To'] == 'candidate@example.test'
    assert '2026-11-01T09:00:00Z' in message.get_body(preferencelist=('plain',)).get_content()
    with pytest.raises(ValueError):
        module.send_confirmation(session, recipient='candidate@example.test\r\nBcc: other@example.test',
                                 start='2026-11-01T09:00:00Z', end='2026-11-01T10:00:00Z')
    assert len(session.calls) == 1


def test_ambiguous_google_failure_is_never_automatically_retried():
    module = provider()
    session = Session(TimeoutError('sensitive provider details'))
    result = module.send_confirmation(session, recipient='candidate@example.test',
        start='2026-11-01T09:00:00Z', end='2026-11-01T10:00:00Z')
    assert result == {'status': 'uncertain', 'provider_id': None}
    assert len(session.calls) == 1

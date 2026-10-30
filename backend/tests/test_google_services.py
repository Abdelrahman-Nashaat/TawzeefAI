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
    session = Session(Response(200, {'id': 'abc123', 'etag': '"event-version"'}))
    result = module.create_calendar_event(session, event_id='abc123',
        start='2026-11-01T09:00:00Z', end='2026-11-01T10:00:00Z')
    assert result == {'status': 'confirmed', 'provider_id': 'abc123', 'etag': '"event-version"'}
    method, url, request = session.calls[0]
    assert method == 'POST' and url == 'https://www.googleapis.com/calendar/v3/calendars/primary/events'
    assert request['params'] == {'sendUpdates': 'none'} and request['allow_redirects'] is False
    assert request['json']['id'] == 'abc123' and 'attendees' not in request['json']
    assert request['json']['extendedProperties']['private']['tawzeefai_delivery'] == 'abc123'
    assert request['json']['start']['dateTime'] == '2026-11-01T09:00:00Z'


def test_calendar_success_without_usable_version_is_uncertain():
    session = Session(Response(200, {'id':'abc123'}))
    result = provider().create_calendar_event(session, event_id='abc123',
        start='2026-11-01T09:00:00Z', end='2026-11-01T10:00:00Z')
    assert result == {'status':'uncertain', 'provider_id':None}
    assert len(session.calls) == 1


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


class SequenceSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def valid_event():
    return {'id': 'abc123', 'etag': '"original"', 'summary': 'TawzeefAI interview',
            'status': 'confirmed', 'visibility': 'private',
            'start': {'dateTime': '2026-11-01T09:00:00Z'},
            'end': {'dateTime': '2026-11-01T10:00:00Z'},
            'extendedProperties': {'private': {'tawzeefai_delivery': 'abc123'}}}


def remove(session):
    return provider().remove_calendar_event(session, event_id='abc123', etag='"original"',
        start='2026-11-01T09:00:00+00:00', end='2026-11-01T10:00:00+00:00')


def test_remove_calendar_verifies_identity_then_conditionally_deletes():
    session = SequenceSession(Response(200, valid_event()), Response(204, {}))
    assert remove(session) == {'status': 'removed'}
    assert [call[0] for call in session.calls] == ['GET', 'DELETE']
    _, url, options = session.calls[1]
    assert url.endswith('/events/abc123') and options['headers']['If-Match'] == '"original"'
    assert options['params'] == {'sendUpdates': 'none'} and not options['allow_redirects']


def test_calendar_fractional_seconds_are_sent_but_google_may_return_seconds_only():
    original_start = '2026-11-01T09:00:00.123456Z'
    original_end = '2026-11-01T10:00:00.987654Z'
    created = Session(Response(200, {'id': 'abc123', 'etag': '"original"'}))
    assert provider().create_calendar_event(created, event_id='abc123', start=original_start, end=original_end)['status'] == 'confirmed'
    assert created.calls[0][2]['json']['start']['dateTime'] == original_start
    assert created.calls[0][2]['json']['end']['dateTime'] == original_end
    deletion = SequenceSession(Response(200, valid_event()), Response(204, {}))
    result = provider().remove_calendar_event(deletion, event_id='abc123', etag='"original"',
                                               start=original_start, end=original_end)
    assert result == {'status': 'removed'}
    assert [call[0] for call in deletion.calls] == ['GET', 'DELETE']


@pytest.mark.parametrize('returned', ['2026-11-01T09:00:01Z', '2026-11-01T09:00:00.500Z'])
def test_calendar_fractional_normalization_does_not_allow_changed_time(returned):
    event = valid_event()
    event['start']['dateTime'] = returned
    session = SequenceSession(Response(200, event))
    result = provider().remove_calendar_event(session, event_id='abc123', etag='"original"',
        start='2026-11-01T09:00:00.123456Z', end='2026-11-01T10:00:00.987654Z')
    assert result == {'status': 'uncertain'}
    assert [call[0] for call in session.calls] == ['GET']


def test_calendar_seconds_comparison_uses_absolute_time_and_accepts_exact_fraction():
    event = valid_event()
    event['start']['dateTime'] = '2026-11-01T11:00:00+02:00'
    event['end']['dateTime'] = '2026-11-01T12:00:00.987654+02:00'
    session = SequenceSession(Response(200, event), Response(204, {}))
    result = provider().remove_calendar_event(session, event_id='abc123', etag='"original"',
        start='2026-11-01T09:00:00.123456Z', end='2026-11-01T10:00:00.987654Z')
    assert result == {'status': 'removed'}


@pytest.mark.parametrize(('field', 'returned', 'original_start', 'original_end'), [
    ('end', '2026-11-01T10:00:01Z', '2026-11-01T09:00:00.123456Z', '2026-11-01T10:00:00.987654Z'),
    ('end', '2026-11-01T10:00:00.400Z', '2026-11-01T09:00:00Z', '2026-11-01T10:00:00Z'),
])
def test_calendar_changed_end_or_added_fraction_never_deletes(field, returned, original_start, original_end):
    event = valid_event()
    event[field]['dateTime'] = returned
    session = SequenceSession(Response(200, event))
    assert provider().remove_calendar_event(session, event_id='abc123', etag='"original"',
                                            start=original_start, end=original_end) == {'status': 'uncertain'}
    assert [call[0] for call in session.calls] == ['GET']


def test_calendar_delete_precondition_failure_remains_uncertain():
    session = SequenceSession(Response(200, valid_event()), Response(412, {}))
    assert provider().remove_calendar_event(session, event_id='abc123', etag='"original"',
        start='2026-11-01T09:00:00.123456Z', end='2026-11-01T10:00:00.987654Z') == {'status': 'uncertain'}
    assert [call[0] for call in session.calls] == ['GET', 'DELETE']


@pytest.mark.parametrize('changes', [
    {'id': 'wrong'}, {'etag': '"changed"'}, {'summary': 'changed'},
    {'start': {'dateTime': '2026-11-02T09:00:00Z'}},
    {'extendedProperties': {'private': {'tawzeefai_delivery': 'wrong'}}},
    {'attendees': [{'email': 'person@example.test'}]}, {'status': 'cancelled'}, {'visibility': 'public'},
])
def test_remove_calendar_refuses_changed_or_unverified_event(changes):
    event = valid_event()
    event.update(changes)
    session = SequenceSession(Response(200, event))
    assert remove(session) == {'status': 'uncertain'}
    assert [call[0] for call in session.calls] == ['GET']


@pytest.mark.parametrize('response', [Response(404, {}), Response(410, {}), TimeoutError('private')])
def test_remove_calendar_does_not_assume_missing_event(response):
    session = SequenceSession(response)
    assert remove(session) == {'status': 'uncertain'}
    assert [call[0] for call in session.calls] == ['GET']


@pytest.mark.parametrize('response', [Response(412, {}), Response(500, {}), TimeoutError('private')])
def test_remove_calendar_does_not_retry_ambiguous_deletion(response):
    session = SequenceSession(Response(200, valid_event()), response)
    assert remove(session) == {'status': 'uncertain'}
    assert [call[0] for call in session.calls] == ['GET', 'DELETE']


def test_ambiguous_google_failure_is_never_automatically_retried():
    module = provider()
    session = Session(TimeoutError('sensitive provider details'))
    result = module.send_confirmation(session, recipient='candidate@example.test',
        start='2026-11-01T09:00:00Z', end='2026-11-01T10:00:00Z')
    assert result == {'status': 'uncertain', 'provider_id': None}
    assert len(session.calls) == 1

"""The read-only inspection fails closed for missing or changed events."""
from app.google_services import inspect_calendar_event
from app.google_templates import CALENDAR_SUMMARY

START = '2027-01-01T10:00:00+00:00'
END = '2027-01-01T11:00:00+00:00'
ID = 'abcdef123456'


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        return self.body


class Session:
    def __init__(self, response):
        self.response = response
        self.methods = []

    def request(self, method, *_args, **_kwargs):
        self.methods.append(method)
        return self.response


def event():
    return {'id': ID, 'etag': '"v1"', 'summary': CALENDAR_SUMMARY, 'status': 'confirmed',
            'visibility': 'private', 'start': {'dateTime': START}, 'end': {'dateTime': END},
            'extendedProperties': {'private': {'tawzeefai_delivery': ID}}}


def test_exact_event_and_untrusted_provider_responses():
    session = Session(Response(200, event()))
    assert inspect_calendar_event(session, event_id=ID, start=START, end=END) == {
        'status': 'confirmed', 'event_id': ID, 'etag': '"v1"'}
    assert session.methods == ['GET']
    for status, body in [(404, {}), (403, {}), (200, {}), (200, None)]:
        assert inspect_calendar_event(Session(Response(status, body)), event_id=ID, start=START, end=END) == {'status': 'uncertain'}
    for field, value in [('etag', 'bad'), ('id', 'other'), ('attendees', [{'email': 'x@example.invalid'}]),
                         ('visibility', 'public'), ('status', 'cancelled')]:
        changed = event()
        changed[field] = value
        assert inspect_calendar_event(Session(Response(200, changed)), event_id=ID, start=START, end=END) == {'status': 'uncertain'}

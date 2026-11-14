"""Narrow Google Calendar and Gmail API calls, invoked only by explicit action.

The caller supplies an authorized session. No calls are retried here: a timeout
can mean the provider completed an operation even though no reply was received.
"""

import base64
import re
from datetime import datetime
from email import policy
from email.message import EmailMessage

import requests

from app.google_templates import (
    CALENDAR_SUMMARY,
    _times,
    cancellation_content,
    confirmation_content,
)

CALENDAR_URL = 'https://www.googleapis.com/calendar/v3/calendars/primary/events'
GMAIL_URL = 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send'
_EVENT_ID = re.compile(r'^[0-9a-v]{5,1024}$')
_EMAIL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$')
_ETAG = re.compile(r'^"[^"\r\n]{1,256}"$')


def _calendar_time_matches(actual: str, expected: str) -> bool:
    """Match exactly, or allow Google to omit the original fractional second."""
    observed = datetime.fromisoformat(actual)
    original = datetime.fromisoformat(expected)
    return observed == original or (observed.microsecond == 0 and observed == original.replace(microsecond=0))


def _post(session, url: str, payload: dict, *, params: dict | None = None,
          expected_id: str | None = None, require_etag: bool = False) -> dict:
    try:
        response = session.request('POST', url, json=payload, params=params,
                                   timeout=20, allow_redirects=False)
    except (requests.RequestException, OSError, TimeoutError):
        return {'status': 'uncertain', 'provider_id': None}
    if 200 <= response.status_code < 300:
        try:
            body = response.json()
            provider_id = body['id']
            if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 1024:
                raise ValueError('Invalid provider ID')
            if expected_id is not None and provider_id != expected_id:
                raise ValueError('Unexpected provider ID')
            etag = body.get('etag') if require_etag else None
            if require_etag and (not isinstance(etag, str) or not _ETAG.fullmatch(etag)):
                raise ValueError('Missing event version')
        except (ValueError, TypeError, KeyError):
            return {'status': 'uncertain', 'provider_id': None}
        result = {'status': 'confirmed', 'provider_id': provider_id}
        if require_etag:
            result['etag'] = etag
        return result
    # A timeout, conflict, rate limit, redirect, or server failure can be
    # ambiguous. Do not retry an email automatically or expose provider errors.
    if 400 <= response.status_code < 500 and response.status_code not in (408, 409, 429):
        return {'status': 'rejected', 'provider_id': None}
    return {'status': 'uncertain', 'provider_id': None}


def create_calendar_event(session, *, event_id: str, start: str, end: str) -> dict:
    if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
        raise ValueError('Invalid Google Calendar event ID')
    start, end = _times(start, end)
    event = {'id': event_id, 'summary': CALENDAR_SUMMARY, 'visibility': 'private',
             'start': {'dateTime': start}, 'end': {'dateTime': end},
             'extendedProperties': {'private': {'tawzeefai_delivery': event_id}}}
    # No attendee list; sending invitations is a separate explicit action.
    return _post(session, CALENDAR_URL, event, params={'sendUpdates': 'none'},
                 expected_id=event_id, require_etag=True)


def inspect_calendar_event(session, *, event_id: str, start: str, end: str) -> dict:
    """Find our exact private event; absence or a mismatch proves nothing."""
    if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
        raise ValueError('Invalid Google Calendar event ID')
    start, end = _times(start, end)
    try:
        response = session.request('GET', f'{CALENDAR_URL}/{event_id}', timeout=20, allow_redirects=False)
        if response.status_code != 200:
            return {'status': 'uncertain'}
        event = response.json()
        actual_start, actual_end = _times(event['start']['dateTime'], event['end']['dateTime'])
        etag = event.get('etag')
        if (event.get('id') != event_id or not isinstance(etag, str) or not _ETAG.fullmatch(etag)
                or event.get('summary') != CALENDAR_SUMMARY or event.get('status') != 'confirmed'
                or event.get('visibility') != 'private' or event.get('attendees')
                or not _calendar_time_matches(actual_start, start) or not _calendar_time_matches(actual_end, end)
                or event['extendedProperties']['private']['tawzeefai_delivery'] != event_id):
            return {'status': 'uncertain'}
        return {'status': 'confirmed', 'event_id': event_id, 'etag': etag}
    except (requests.RequestException, OSError, TimeoutError, ValueError, TypeError, KeyError, AttributeError):
        return {'status': 'uncertain'}


def remove_calendar_event(session, *, event_id: str, etag: str, start: str, end: str) -> dict:
    """Verify our exact event before a single conditional delete.

    The caller must separately verify the credential is bound to the original
    delivery and serialize this operation with local cancellation. A missing or
    inaccessible event is not proof of removal from the original calendar.
    """
    if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
        raise ValueError('Invalid Google Calendar event ID')
    if not isinstance(etag, str) or not _ETAG.fullmatch(etag):
        raise ValueError('Invalid Google Calendar event version')
    start, end = _times(start, end)
    url = f'{CALENDAR_URL}/{event_id}'
    try:
        response = session.request('GET', url, timeout=20, allow_redirects=False)
        if response.status_code != 200:
            return {'status': 'uncertain'}
        event = response.json()
        if not isinstance(event, dict):
            return {'status': 'uncertain'}
        actual_start, actual_end = _times(event['start']['dateTime'], event['end']['dateTime'])
        if (event.get('id') != event_id or event.get('etag') != etag
                or event.get('summary') != CALENDAR_SUMMARY
                or event.get('status') != 'confirmed' or event.get('visibility') != 'private'
                or event.get('attendees') or not _calendar_time_matches(actual_start, start)
                or not _calendar_time_matches(actual_end, end)
                or event['extendedProperties']['private']['tawzeefai_delivery'] != event_id):
            return {'status': 'uncertain'}
    except (requests.RequestException, OSError, TimeoutError, ValueError, TypeError, KeyError):
        return {'status': 'uncertain'}
    try:
        response = session.request('DELETE', url, headers={'If-Match': etag},
                                   params={'sendUpdates': 'none'}, timeout=20, allow_redirects=False)
    except (requests.RequestException, OSError, TimeoutError):
        return {'status': 'uncertain'}
    return {'status': 'removed' if response.status_code == 204 else 'uncertain'}


def send_confirmation(session, *, recipient: str, start: str, end: str) -> dict:
    if not isinstance(recipient, str) or len(recipient) > 254 or not _EMAIL.fullmatch(recipient):
        raise ValueError('A single valid email recipient is required')
    subject, body = confirmation_content(start, end)
    message = EmailMessage(policy=policy.SMTP)
    message['To'] = recipient
    message['Subject'] = subject
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode('ascii').rstrip('=')
    return _post(session, GMAIL_URL, {'raw': raw})


def send_cancellation(session, *, recipient: str, start: str, end: str) -> dict:
    """Send one reviewed cancellation message; never retry ambiguous outcomes."""
    if not isinstance(recipient, str) or len(recipient) > 254 or not _EMAIL.fullmatch(recipient):
        raise ValueError('A single valid email recipient is required')
    subject, body = cancellation_content(start, end)
    message = EmailMessage(policy=policy.SMTP)
    message['To'] = recipient
    message['Subject'] = subject
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode('ascii').rstrip('=')
    return _post(session, GMAIL_URL, {'raw': raw})

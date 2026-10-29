"""Narrow Google Calendar and Gmail API calls, invoked only by explicit action.

The caller supplies an authorized session. No calls are retried here: a timeout
can mean the provider completed an operation even though no reply was received.
"""

import base64
import re
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage

import requests

CALENDAR_URL = 'https://www.googleapis.com/calendar/v3/calendars/primary/events'
GMAIL_URL = 'https://gmail.googleapis.com/gmail/v1/users/me/messages/send'
_EVENT_ID = re.compile(r'^[0-9a-v]{5,1024}$')
_EMAIL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$')


def _times(start: str, end: str) -> tuple[str, str]:
    try:
        first = datetime.fromisoformat(start)
        last = datetime.fromisoformat(end)
        if first.tzinfo is None or last.tzinfo is None or first.utcoffset() is None or last.utcoffset() is None:
            raise ValueError('Timezone required')
        if last <= first:
            raise ValueError('Invalid interval')
    except (AttributeError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError('An ordered timezone-aware interview interval is required') from exc
    return tuple(value.astimezone(UTC).isoformat().replace('+00:00', 'Z') for value in (first, last))


def _post(session, url: str, payload: dict, *, params: dict | None = None,
          expected_id: str | None = None) -> dict:
    try:
        response = session.request('POST', url, json=payload, params=params,
                                   timeout=20, allow_redirects=False)
    except (requests.RequestException, OSError, TimeoutError):
        return {'status': 'uncertain', 'provider_id': None}
    if 200 <= response.status_code < 300:
        try:
            provider_id = response.json()['id']
            if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 1024:
                raise ValueError('Invalid provider ID')
            if expected_id is not None and provider_id != expected_id:
                raise ValueError('Unexpected provider ID')
        except (ValueError, TypeError, KeyError):
            return {'status': 'uncertain', 'provider_id': None}
        return {'status': 'confirmed', 'provider_id': provider_id}
    # A timeout, conflict, rate limit, redirect, or server failure can be
    # ambiguous. Do not retry an email automatically or expose provider errors.
    if 400 <= response.status_code < 500 and response.status_code not in (408, 409, 429):
        return {'status': 'rejected', 'provider_id': None}
    return {'status': 'uncertain', 'provider_id': None}


def create_calendar_event(session, *, event_id: str, start: str, end: str) -> dict:
    if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
        raise ValueError('Invalid Google Calendar event ID')
    start, end = _times(start, end)
    event = {'id': event_id, 'summary': 'TawzeefAI interview', 'visibility': 'private',
             'start': {'dateTime': start}, 'end': {'dateTime': end}}
    # No attendee list; sending invitations is a separate explicit action.
    return _post(session, CALENDAR_URL, event, params={'sendUpdates': 'none'}, expected_id=event_id)


def send_confirmation(session, *, recipient: str, start: str, end: str) -> dict:
    if not isinstance(recipient, str) or len(recipient) > 254 or not _EMAIL.fullmatch(recipient):
        raise ValueError('A single valid email recipient is required')
    start, end = _times(start, end)
    message = EmailMessage(policy=policy.SMTP)
    message['To'] = recipient
    message['Subject'] = 'Interview confirmation'
    message.set_content(f'Your interview is scheduled from {start} to {end}.\n')
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode('ascii').rstrip('=')
    return _post(session, GMAIL_URL, {'raw': raw})

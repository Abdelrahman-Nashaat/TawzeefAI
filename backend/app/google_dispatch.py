"""Prepare request-local, credential-bound Google actions; never enabled by default."""

import hashlib
import json

from app import google_services


def _authorized_session(service):
    # The Google dependencies and Windows credential vault are optional.
    from app.google_auth import authorized_session
    return authorized_session(service)


def _binding(session):
    """Fail closed after reauthorization/rotation, including to the same user."""
    credentials = getattr(session, 'credentials', None)
    client_id = getattr(credentials, 'client_id', None)
    refresh_token = getattr(credentials, 'refresh_token', None)
    if not isinstance(client_id, str) or not client_id or not isinstance(refresh_token, str) or not refresh_token:
        raise RuntimeError('Google calendar identity unavailable')
    payload = json.dumps(['tawzeefai-google-calendar-v1', client_id, refresh_token],
                         ensure_ascii=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def prepare_dispatch(kind):
    if kind not in ('calendar', 'email', 'cancellation_email'):
        raise ValueError('Unsupported Google service')
    session = _authorized_session('calendar' if kind == 'calendar' else 'gmail')
    binding = _binding(session) if kind == 'calendar' else None
    prepared_kind = kind

    def dispatch(*, kind: str, event_id: str | None, start_at: str, end_at: str, recipient: str | None):
        if kind != prepared_kind:
            raise ValueError('Google delivery does not match prepared service')
        if kind == 'calendar' and binding is not None and recipient is None:
            return google_services.create_calendar_event(session, event_id=event_id, start=start_at, end=end_at)
        if kind == 'email' and binding is None and event_id is None:
            return google_services.send_confirmation(session, recipient=recipient, start=start_at, end=end_at)
        if kind == 'cancellation_email' and binding is None and event_id is None:
            return google_services.send_cancellation(session, recipient=recipient, start=start_at, end=end_at)
        raise ValueError('Google delivery does not match prepared service')

    if binding is not None:
        dispatch.google_credential_binding = binding
    return dispatch


def prepare_calendar_removal():
    session = _authorized_session('calendar')
    binding = _binding(session)

    def remove(*, event_id: str, etag: str, start: str, end: str):
        return google_services.remove_calendar_event(session, event_id=event_id, etag=etag, start=start, end=end)

    remove.google_credential_binding = binding
    return remove


def prepare_calendar_inspection():
    session = _authorized_session('calendar')
    binding = _binding(session)

    def inspect(*, event_id: str, start: str, end: str):
        return google_services.inspect_calendar_event(session, event_id=event_id, start=start, end=end)

    inspect.google_credential_binding = binding
    return inspect

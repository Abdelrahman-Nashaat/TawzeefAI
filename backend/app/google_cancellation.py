"""Explicit, durable cancellation notices for previously confirmed emails.

No notice is sent automatically. HR must review the exact recipient and content
for each attempt. Ambiguous results require explicit external reconciliation.
"""

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from app.auth import User, require_roles
from app.db import connect
from app.google_outbox import _actor, _audit, _interview
from app.google_templates import cancellation_content

router = APIRouter(prefix='/api/google')
HR = Annotated[User, Depends(require_roles('hr'))]


@contextmanager
def _notice_lock(path: str | Path):
    """Hold an OS lock throughout dispatch; a crashed process releases it.

    All app processes using this local SQLite database must share its lock file;
    do not delete it while any instance is running.
    The lock does not verify what happened at the provider.
    """
    handle = None
    try:
        canonical = Path(path).resolve()
        handle = open(f'{canonical}.cancellation-notices.lock', 'a+b')  # noqa: SIM115 - closed in finally.
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if handle is not None:
            handle.close()
        raise HTTPException(status_code=409, detail='Another cancellation notice operation is in progress') from exc
    try:
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def initialize_cancellation_notices(path: str | Path) -> None:
    with connect(path) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS cancellation_notices (
            id INTEGER PRIMARY KEY,
            interview_id INTEGER NOT NULL REFERENCES interviews(id),
            confirmation_id INTEGER NOT NULL REFERENCES google_deliveries(id),
            application_id INTEGER NOT NULL REFERENCES applications(id),
            recipient TEXT NOT NULL, start_at TEXT NOT NULL, end_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('sending','confirmed','rejected','uncertain','verified_sent','verified_not_sent')),
            provider_id TEXT, created_by INTEGER NOT NULL REFERENCES users(id),
            resolved_by INTEGER REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )''')
        db.execute('CREATE INDEX IF NOT EXISTS cancellation_notices_interview ON cancellation_notices(interview_id,id)')


class SendNotice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmed: StrictBool
    fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')

    @model_validator(mode='after')
    def reviewed(self):
        if not self.confirmed:
            raise ValueError('Explicit review is required')
        return self


class ResolveNotice(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmed: StrictBool
    outcome: Literal['sent', 'not_sent']

    @model_validator(mode='after')
    def reviewed(self):
        if not self.confirmed:
            raise ValueError('Explicit external verification is required')
        return self


def _snapshot(db, interview_id: int) -> dict:
    interview = _interview(db, interview_id)
    if interview['status'] != 'cancelled':
        raise HTTPException(status_code=409, detail='The interview must be cancelled first')
    original = db.execute("""SELECT id,application_id,recipient,start_at,end_at FROM google_deliveries
        WHERE interview_id=? AND kind='email' AND state='confirmed'""", (interview_id,)).fetchone()
    if original is None:
        raise HTTPException(status_code=409, detail='A confirmed original email is required')
    if (original['application_id'] != interview['application_id'] or original['start_at'] != interview['start_at']
            or original['end_at'] != interview['end_at']):
        raise HTTPException(status_code=409, detail='Original confirmation differs from the interview')
    try:
        subject, body = cancellation_content(interview['start_at'], interview['end_at'])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail='Interview time is invalid') from exc
    last = db.execute('SELECT id,state FROM cancellation_notices WHERE interview_id=? ORDER BY id DESC LIMIT 1',
                      (interview_id,)).fetchone()
    fields = {'interview_id': interview_id, 'application_id': interview['application_id'],
              'confirmation_id': original['id'], 'recipient': original['recipient'],
              'start_at': interview['start_at'], 'end_at': interview['end_at'],
              'subject': subject, 'body': body,
              'last_notice': dict(last) if last else None}
    fields['can_send'] = last is None or last['state'] in ('rejected', 'verified_not_sent')
    data = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    fields['fingerprint'] = hashlib.sha256(data).hexdigest()
    fields['notice'] = ('The recipient is the address used for the earlier confirmation. '
                        'Verify it before sending; the application does not verify address ownership. '
                        'The current Gmail account may differ from the account that sent the confirmation; '
                        'the application does not verify the original sender.')
    return fields


@router.get('/interviews/{interview_id}/cancellation-notice')
def preview_notice(interview_id: int, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _actor(db, request, user)
        result = _snapshot(db, interview_id)
        _actor(db, request, user)
        return result


@router.post('/interviews/{interview_id}/cancellation-notice/send')
def send_notice(interview_id: int, payload: SendNotice, request: Request, user: HR):
    with _notice_lock(request.app.state.database_path):
        return _send_notice_locked(interview_id, payload, request, user)


def _send_notice_locked(interview_id: int, payload: SendNotice, request: Request, user: User):
    path = request.app.state.database_path
    with connect(path) as db:
        db.execute('BEGIN')
        _actor(db, request, user)
        preview = _snapshot(db, interview_id)
        if not preview['can_send'] or preview['fingerprint'] != payload.fingerprint:
            raise HTTPException(status_code=409, detail='Notice changed; review the current preview')
    preflight = getattr(request.app.state, 'google_preflight', None)
    if not callable(preflight):
        raise HTTPException(status_code=503, detail='Google email is not configured')
    try:
        dispatch = preflight('cancellation_email')
        if not callable(dispatch):
            raise TypeError('Invalid dispatcher')
    except Exception as exc:
        raise HTTPException(status_code=503, detail='Google authorization is not available') from exc
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        _actor(db, request, user)
        current = _snapshot(db, interview_id)
        if not current['can_send'] or current['fingerprint'] != payload.fingerprint:
            raise HTTPException(status_code=409, detail='Notice changed; review the current preview')
        cursor = db.execute('''INSERT INTO cancellation_notices
            (interview_id,confirmation_id,application_id,recipient,start_at,end_at,state,created_by)
            VALUES (?,?,?,?,?,?,'sending',?)''',
            (interview_id, current['confirmation_id'], current['application_id'], current['recipient'],
             current['start_at'], current['end_at'], user.id))
        notice_id = cursor.lastrowid
        _audit(db, current['application_id'], user.id, notice_id, 'email', 'cancellation_notice_started')
    try:
        with connect(path) as db:
            db.execute('BEGIN')
            _actor(db, request, user)
            interview = _interview(db, interview_id)
            original = db.execute("SELECT * FROM google_deliveries WHERE id=? AND state='confirmed' AND kind='email'",
                                  (current['confirmation_id'],)).fetchone()
            if (interview['status'] != 'cancelled' or original is None or original['interview_id'] != interview_id
                    or original['application_id'] != interview['application_id']
                    or interview['application_id'] != current['application_id']
                    or original['recipient'] != current['recipient'] or original['start_at'] != current['start_at']
                    or original['end_at'] != current['end_at'] or interview['start_at'] != current['start_at']
                    or interview['end_at'] != current['end_at']):
                raise HTTPException(status_code=409, detail='Interview or original confirmation changed')
    except HTTPException:
        with connect(path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE cancellation_notices SET state='rejected' WHERE id=? AND state='sending'", (notice_id,))
            _audit(db, current['application_id'], user.id, notice_id, 'email', 'cancellation_notice_preflight_rejected')
        raise
    try:
        result = dispatch(kind='cancellation_email', event_id=None, recipient=current['recipient'],
                          start_at=current['start_at'], end_at=current['end_at'])
        state = result.get('status') if isinstance(result, dict) else None
        provider_id = result.get('provider_id') if isinstance(result, dict) else None
        if state not in ('confirmed', 'rejected', 'uncertain'):
            state = 'uncertain'
        if state == 'confirmed' and (not isinstance(provider_id, str) or not provider_id or len(provider_id) > 1024):
            state = 'uncertain'
        if state != 'confirmed':
            provider_id = None
    except Exception:  # noqa: BLE001 - the external side effect may have succeeded.
        state, provider_id = 'uncertain', None
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("""UPDATE cancellation_notices SET state=?,provider_id=?,
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND state='sending'""",
            (state, provider_id, notice_id))
        _audit(db, current['application_id'], user.id, notice_id, 'email', 'cancellation_notice_result')
    with connect(path) as db:
        db.execute('BEGIN')
        _actor(db, request, user)
        return {'id': notice_id, 'state': state}


@router.post('/cancellation-notices/{notice_id}/resolve')
def resolve_notice(notice_id: int, payload: ResolveNotice, request: Request, user: HR):
    """Record HR's external verification only while no notice send is active."""
    with _notice_lock(request.app.state.database_path):
        return _resolve_notice_locked(notice_id, payload, request, user)


def _resolve_notice_locked(notice_id: int, payload: ResolveNotice, request: Request, user: User):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _actor(db, request, user)
        row = db.execute('SELECT * FROM cancellation_notices WHERE id=?', (notice_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='Notice not found')
        if row['state'] not in ('uncertain', 'sending'):
            raise HTTPException(status_code=409, detail='Only an uncertain or interrupted notice can be resolved')
        state = 'verified_sent' if payload.outcome == 'sent' else 'verified_not_sent'
        db.execute("""UPDATE cancellation_notices SET state=?,resolved_by=?,
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?""", (state, user.id, notice_id))
        _audit(db, row['application_id'], user.id, notice_id, 'email', f'cancellation_notice_{state}')
        _actor(db, request, user)
        return {'id': notice_id, 'state': state}

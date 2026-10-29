"""Explicit, durable Google delivery requests; no provider is configured here.

This module never sends automatically. A caller must inject a dispatcher, and
HR must explicitly confirm both preparation and dispatch. A claimed request is
never retried automatically; a crash after claiming leaves it in ``sending``.
Cancellation routes must consult ``cancellation_block_reason`` in their own
write transaction before changing an interview, otherwise a send can race a
cancellation. Google Calendar and Gmail delivery are distinct from local booking.
"""

import json
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from app.auth import COOKIE_NAME, User, require_roles, token_digest
from app.db import connect

router = APIRouter(prefix='/api/google')
HR = Annotated[User, Depends(require_roles('hr'))]
_EMAIL = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$')


def initialize_google_outbox(path: str | Path) -> None:
    with connect(path) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS google_deliveries (
            id INTEGER PRIMARY KEY,
            interview_id INTEGER NOT NULL REFERENCES interviews(id),
            application_id INTEGER NOT NULL REFERENCES applications(id),
            kind TEXT NOT NULL CHECK (kind IN ('calendar', 'email')),
            recipient TEXT,
            event_id TEXT,
            start_at TEXT NOT NULL,
            end_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('pending','sending','confirmed','rejected','uncertain')),
            provider_id TEXT,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE(interview_id, kind),
            CHECK ((kind='email' AND recipient IS NOT NULL AND event_id IS NULL)
                OR (kind='calendar' AND recipient IS NULL AND event_id IS NOT NULL))
        );
        CREATE INDEX IF NOT EXISTS google_deliveries_interview ON google_deliveries(interview_id, state);
        ''')


class Prepare(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['calendar', 'email']
    recipient: str | None = Field(default=None, max_length=254)
    confirmed: StrictBool

    @model_validator(mode='after')
    def valid(self):
        if not self.confirmed:
            raise ValueError('Explicit review is required')
        if self.kind == 'calendar' and self.recipient is not None:
            raise ValueError('Calendar event does not include a recipient')
        if self.kind == 'email' and (self.recipient is None or not _EMAIL.fullmatch(self.recipient)):
            raise ValueError('One valid recipient is required')
        return self


class Confirm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmed: StrictBool

    @model_validator(mode='after')
    def valid(self):
        if not self.confirmed:
            raise ValueError('Explicit confirmation is required')
        return self


def _actor(db, request: Request, user: User) -> None:
    token = request.cookies.get(COOKIE_NAME, '')
    row = db.execute('''SELECT u.id, u.username, u.role, u.active FROM sessions s
        JOIN users u ON u.id = s.user_id WHERE s.user_id = ? AND s.token_hash = ? AND s.expires_at > ?''',
        (user.id, token_digest(token), int(time.time()))).fetchone()
    if row is None or not row['active'] or row['username'] != user.username:
        raise HTTPException(status_code=401, detail='Authentication required')
    if row['role'] != 'hr':
        raise HTTPException(status_code=403, detail='HR access required')


def _interview(db, interview_id: int):
    row = db.execute('SELECT * FROM interviews WHERE id=?', (interview_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail='Interview not found')
    return row


def _eligible(interview) -> None:
    if interview['status'] != 'scheduled':
        raise HTTPException(status_code=409, detail='Interview is not scheduled')
    try:
        start = datetime.fromisoformat(interview['start_at'])
        end = datetime.fromisoformat(interview['end_at'])
        if start.tzinfo is None or end.tzinfo is None or start.utcoffset() is None or end.utcoffset() is None:
            raise ValueError('Missing timezone')
        if start <= datetime.now(UTC) or end <= start:
            raise ValueError('Invalid interval')
    except (ValueError, TypeError, AttributeError, OverflowError) as exc:
        raise HTTPException(status_code=409, detail='Interview time is invalid or has passed') from exc


def _audit(db, application_id: int, actor_id: int, delivery_id: int, kind: str, action: str) -> None:
    details = json.dumps({'delivery_id': delivery_id, 'kind': kind})
    db.execute('INSERT INTO audit_events(application_id,actor_id,action,details) VALUES (?,?,?,?)',
               (application_id, actor_id, action, details))


def _public(row) -> dict:
    return {key: row[key] for key in ('id', 'interview_id', 'kind', 'recipient', 'event_id',
                                       'start_at', 'end_at', 'state', 'provider_id', 'created_at', 'updated_at')}


def cancellation_block_reason(db, interview_id: int) -> str | None:
    """Call while holding the cancellation transaction's SQLite write lock.

    A sent or uncertain Calendar event requires explicit external reconciliation;
    an in-flight or uncertain message similarly cannot safely be treated as absent.
    """
    row = db.execute('''SELECT kind, state FROM google_deliveries WHERE interview_id = ?
        AND (state IN ('sending','uncertain') OR (kind = 'calendar' AND state = 'confirmed'))
        ORDER BY CASE WHEN state = 'sending' THEN 0 ELSE 1 END, id LIMIT 1''', (interview_id,)).fetchone()
    if row is None:
        return None
    return 'google_delivery_in_flight_or_unresolved' if row['state'] != 'confirmed' else 'google_calendar_requires_reconciliation'


@router.get('/interviews/{interview_id}/deliveries')
def list_deliveries(interview_id: int, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _actor(db, request, user)
        _interview(db, interview_id)
        return [_public(row) for row in db.execute(
            'SELECT * FROM google_deliveries WHERE interview_id=? ORDER BY id', (interview_id,))]


@router.post('/interviews/{interview_id}/deliveries', status_code=201)
def prepare_delivery(interview_id: int, payload: Prepare, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _actor(db, request, user)
        interview = _interview(db, interview_id)
        _eligible(interview)
        existing = db.execute('SELECT * FROM google_deliveries WHERE interview_id=? AND kind=?',
                              (interview_id, payload.kind)).fetchone()
        if existing:
            if existing['state'] == 'pending' and existing['recipient'] == payload.recipient:
                return _public(existing)
            raise HTTPException(status_code=409, detail='Delivery already exists and needs review')
        event_id = uuid.uuid4().hex if payload.kind == 'calendar' else None
        cursor = db.execute('''INSERT INTO google_deliveries
            (interview_id,application_id,kind,recipient,event_id,start_at,end_at,state,created_by)
            VALUES (?,?,?,?,?,?,?,'pending',?)''', (interview_id, interview['application_id'], payload.kind,
            payload.recipient, event_id, interview['start_at'], interview['end_at'], user.id))
        _audit(db, interview['application_id'], user.id, cursor.lastrowid, payload.kind, 'google_delivery_prepared')
        return _public(db.execute('SELECT * FROM google_deliveries WHERE id=?', (cursor.lastrowid,)).fetchone())


@router.post('/deliveries/{delivery_id}/send')
def send_delivery(delivery_id: int, payload: Confirm, request: Request, user: HR):
    dispatcher = getattr(request.app.state, 'google_dispatcher', None)
    if not callable(dispatcher):
        raise HTTPException(status_code=503, detail='Google delivery is not configured')
    path = request.app.state.database_path
    # Check credentials outside the write lock and before claiming the draft.
    # Revalidate everything under the lock afterwards because this can race a
    # cancellation, another sender, or session revocation.
    preflight = getattr(request.app.state, 'google_preflight', None)
    if preflight is not None:
        with connect(path) as db:
            db.execute('BEGIN')
            _actor(db, request, user)
            draft = db.execute('SELECT * FROM google_deliveries WHERE id=?', (delivery_id,)).fetchone()
            if draft is None:
                raise HTTPException(status_code=404, detail='Delivery not found')
            if draft['state'] != 'pending':
                raise HTTPException(status_code=409, detail='Delivery cannot be sent again automatically')
            _eligible(_interview(db, draft['interview_id']))
        try:
            prepared = preflight(draft['kind'])
            if prepared is not None:
                if not callable(prepared):
                    raise ValueError('Invalid prepared dispatcher')
                dispatcher = prepared
        except Exception as exc:
            raise HTTPException(status_code=503, detail='Google authorization is not available') from exc
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        _actor(db, request, user)
        row = db.execute('SELECT * FROM google_deliveries WHERE id=?', (delivery_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='Delivery not found')
        if row['state'] != 'pending':
            raise HTTPException(status_code=409, detail='Delivery cannot be sent again automatically')
        interview = _interview(db, row['interview_id'])
        _eligible(interview)
        if row['start_at'] != interview['start_at'] or row['end_at'] != interview['end_at']:
            raise HTTPException(status_code=409, detail='Interview time changed; review the delivery')
        db.execute("UPDATE google_deliveries SET state='sending', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?", (delivery_id,))
        _audit(db, row['application_id'], user.id, row['id'], row['kind'], 'google_delivery_started')
        arguments = {key: row[key] for key in ('kind', 'event_id', 'start_at', 'end_at', 'recipient')}

    # Recheck immediately before the external call. Cancellation must also
    # consult cancellation_block_reason within its own write transaction.
    try:
        with connect(path) as db:
            db.execute('BEGIN')
            _actor(db, request, user)
            interview = _interview(db, row['interview_id'])
            _eligible(interview)
            if row['start_at'] != interview['start_at'] or row['end_at'] != interview['end_at']:
                raise HTTPException(status_code=409, detail='Interview time changed; review the delivery')
    except HTTPException:
        # No external call has occurred, so this specific attempt is rejected.
        with connect(path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE google_deliveries SET state='rejected', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND state='sending'", (delivery_id,))
            _audit(db, row['application_id'], user.id, row['id'], row['kind'], 'google_delivery_preflight_rejected')
        raise

    # Exceptions and invalid provider responses are ambiguous; never retry.
    try:
        result = dispatcher(**arguments)
        state = result.get('status') if isinstance(result, dict) else None
        provider_id = result.get('provider_id') if isinstance(result, dict) else None
        if state not in ('confirmed', 'rejected', 'uncertain'):
            state = 'uncertain'
        if state == 'confirmed' and (not isinstance(provider_id, str) or not provider_id
                                     or len(provider_id) > 1024
                                     or (arguments['kind'] == 'calendar' and provider_id != arguments['event_id'])):
            state = 'uncertain'
        if state != 'confirmed':
            provider_id = None
    except Exception:  # noqa: BLE001 - any provider failure can leave an ambiguous side effect.
        state, provider_id = 'uncertain', None
    with connect(path) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''UPDATE google_deliveries SET state=?,provider_id=?,
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=? AND state='sending' ''',
            (state, provider_id, delivery_id))
        _audit(db, row['application_id'], user.id, row['id'], row['kind'], 'google_delivery_result')
        result = _public(db.execute('SELECT * FROM google_deliveries WHERE id=?', (delivery_id,)).fetchone())
    # Record the provider result even if access was revoked while it was in
    # flight, but don't reveal the recipient or result to a revoked session.
    with connect(path) as db:
        db.execute('BEGIN')
        _actor(db, request, user)
        return result

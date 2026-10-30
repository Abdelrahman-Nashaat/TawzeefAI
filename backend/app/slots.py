"""Human-managed, local-only interview slots with atomic candidate booking."""

import json
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from app.auth import COOKIE_NAME, User, get_current_user, require_roles, token_digest
from app.db import connect
from app.scheduling import interview_view, utc_text

router = APIRouter(prefix='/api')
HR = Annotated[User, Depends(require_roles('hr'))]
Candidate = Annotated[User, Depends(require_roles('candidate'))]
Authenticated = Annotated[User, Depends(get_current_user)]


def initialize_slots(path: str | Path) -> None:
    with connect(path) as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS interview_slots (
                id INTEGER PRIMARY KEY,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                interviewer TEXT NOT NULL,
                interviewer_key TEXT NOT NULL,
                created_by INTEGER NOT NULL REFERENCES users(id),
                interview_id INTEGER UNIQUE REFERENCES interviews(id),
                withdrawn INTEGER NOT NULL DEFAULT 0 CHECK (withdrawn IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE INDEX IF NOT EXISTS slot_time_idx ON interview_slots(start_at, end_at);
            CREATE TABLE IF NOT EXISTS slot_audit_events (
                id INTEGER PRIMARY KEY,
                slot_id INTEGER NOT NULL REFERENCES interview_slots(id),
                actor_id INTEGER NOT NULL REFERENCES users(id),
                action TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
        ''')
        db.execute('BEGIN IMMEDIATE')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(interview_slots)')}
        if 'withdrawn' not in columns:
            db.execute('ALTER TABLE interview_slots ADD COLUMN withdrawn INTEGER NOT NULL DEFAULT 0 CHECK (withdrawn IN (0, 1))')


class SlotCreate(BaseModel):
    start_at: datetime
    end_at: datetime
    interviewer: str = Field(min_length=1, max_length=120)

    @field_validator('start_at', 'end_at')
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('An explicit timezone offset is required')
        return value.astimezone(UTC)

    @field_validator('interviewer')
    @classmethod
    def clean_interviewer(cls, value: str) -> str:
        value = ' '.join(unicodedata.normalize('NFKC', value).split())
        if not value or any(ord(ch) < 32 for ch in value):
            raise ValueError('Interviewer is required')
        return value

    @model_validator(mode='after')
    def validate_range(self):
        if self.end_at <= self.start_at:
            raise ValueError('Slot end must be after its start')
        return self


class BookSlot(BaseModel):
    application_id: int = Field(gt=0)


def owned_advanced_application(db, application_id: int, user: User):
    application = db.execute(
        'SELECT id, candidate_id, status FROM applications WHERE id = ? AND candidate_id = ?',
        (application_id, user.id),
    ).fetchone()
    if application is None:
        raise HTTPException(status_code=404, detail='Application not found')
    if application['status'] != 'advanced':
        raise HTTPException(status_code=409, detail='Application is not eligible for booking')
    return application


def slot_view(row) -> dict:
    return {key: row[key] for key in ('id', 'start_at', 'end_at', 'interviewer')}


def _require_current_role(db, request: Request, user: User, role: str) -> None:
    """Recheck the requesting session inside the serialized write transaction."""
    session = db.execute('''SELECT u.username, u.role FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.user_id=? AND s.token_hash=? AND s.expires_at>? AND u.active=1''',
        (user.id, token_digest(request.cookies.get(COOKIE_NAME, '')), int(time.time()))).fetchone()
    if session is None or session['username'] != user.username:
        raise HTTPException(status_code=401, detail='Authentication required')
    if session['role'] != role:
        raise HTTPException(status_code=403, detail=f'{role.capitalize()} access required')


@router.post('/slots', status_code=201)
def create_slot(payload: SlotCreate, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        return create_slot_record(db, payload, user.id)


def create_slot_record(db, payload: SlotCreate, actor_id: int) -> dict:
    """Create a slot and its audit record inside the caller's transaction."""
    if payload.start_at <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail='Slot start must be in the future')
    cursor = db.execute(
        'INSERT INTO interview_slots (start_at,end_at,interviewer,interviewer_key,created_by) VALUES (?,?,?,?,?)',
        (utc_text(payload.start_at), utc_text(payload.end_at), payload.interviewer,
         payload.interviewer.casefold(), actor_id),
    )
    db.execute('INSERT INTO slot_audit_events (slot_id,actor_id,action) VALUES (?, ?, ?)',
               (cursor.lastrowid, actor_id, 'slot_created'))
    return slot_view(db.execute('SELECT * FROM interview_slots WHERE id = ?', (cursor.lastrowid,)).fetchone())


@router.get('/slots')
def list_slots(request: Request, user: Authenticated, application_id: int | None = None):
    with connect(request.app.state.database_path) as db:
        if user.role == 'hr':
            rows = db.execute('''SELECT s.*, i.status AS interview_status
                FROM interview_slots s LEFT JOIN interviews i ON i.id = s.interview_id
                ORDER BY s.start_at, s.id''').fetchall()
            return [{**slot_view(row), 'booked': row['interview_status'] == 'scheduled',
                     'withdrawn': bool(row['withdrawn'])} for row in rows]
        if user.role != 'candidate':
            raise HTTPException(status_code=403, detail='Access denied')
        if application_id is None or application_id <= 0:
            raise HTTPException(status_code=422, detail='Application is required')
        application = owned_advanced_application(db, application_id, user)
        if db.execute("""SELECT 1 FROM interviews i WHERE i.application_id = ? AND i.status = 'scheduled'
                      AND NOT EXISTS (SELECT 1 FROM interview_completions c WHERE c.interview_id=i.id)""", (application_id,)).fetchone():
            return []
        now = utc_text(datetime.now(UTC))
        rows = db.execute('''SELECT s.* FROM interview_slots s
            LEFT JOIN interviews linked ON linked.id = s.interview_id
            WHERE s.withdrawn = 0 AND s.start_at > ? AND (s.interview_id IS NULL OR linked.status = 'cancelled')
            AND NOT EXISTS (SELECT 1 FROM interviews i WHERE i.status = 'scheduled'
                AND i.start_at < s.end_at AND i.end_at > s.start_at
                AND (i.candidate_id = ? OR i.interviewer_key = s.interviewer_key))
            ORDER BY s.start_at, s.id''', (now, application['candidate_id'])).fetchall()
        return [slot_view(row) for row in rows]


@router.post('/slots/{slot_id}/book', status_code=201)
def book_slot(slot_id: int, payload: BookSlot, request: Request, user: Candidate):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'candidate')
        application = owned_advanced_application(db, payload.application_id, user)
        slot = db.execute('''SELECT s.*, i.status AS interview_status FROM interview_slots s
            LEFT JOIN interviews i ON i.id = s.interview_id WHERE s.id = ?''', (slot_id,)).fetchone()
        if slot is None:
            raise HTTPException(status_code=404, detail='Slot not found')
        now = utc_text(datetime.now(UTC))
        if slot['withdrawn'] or slot['start_at'] <= now or slot['interview_status'] == 'scheduled':
            raise HTTPException(status_code=409, detail='Slot is not available')
        if db.execute("""SELECT 1 FROM interviews i WHERE i.application_id = ? AND i.status = 'scheduled'
                      AND NOT EXISTS (SELECT 1 FROM interview_completions c WHERE c.interview_id=i.id)""", (payload.application_id,)).fetchone():
            raise HTTPException(status_code=409, detail='Application already has an interview')
        conflict = db.execute('''SELECT 1 FROM interviews WHERE status = 'scheduled'
            AND start_at < ? AND end_at > ? AND (candidate_id = ? OR interviewer_key = ?) LIMIT 1''',
            (slot['end_at'], slot['start_at'], application['candidate_id'], slot['interviewer_key'])).fetchone()
        if conflict:
            raise HTTPException(status_code=409, detail='Candidate or interviewer has a conflicting interview')
        cursor = db.execute('''INSERT INTO interviews
            (application_id,candidate_id,start_at,end_at,interviewer,interviewer_key,created_by)
            VALUES (?,?,?,?,?,?,?)''', (application['id'], application['candidate_id'], slot['start_at'],
            slot['end_at'], slot['interviewer'], slot['interviewer_key'], user.id))
        db.execute('UPDATE interview_slots SET interview_id = ? WHERE id = ?', (cursor.lastrowid, slot_id))
        db.execute('INSERT INTO audit_events (application_id,actor_id,action,details) VALUES (?, ?, ?, ?)',
                   (application['id'], user.id, 'interview_scheduled', json.dumps({
                       'interview_id': cursor.lastrowid, 'slot_id': slot_id, 'source': 'candidate_selection',
                   })))
        return interview_view(db.execute('SELECT * FROM interviews WHERE id = ?', (cursor.lastrowid,)).fetchone())


@router.post('/slots/{slot_id}/withdraw')
def withdraw_slot(slot_id: int, request: Request, user: HR):
    """Retire an unbooked slot without cancelling an existing interview."""
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        row = db.execute('''SELECT s.*, i.status AS interview_status FROM interview_slots s
            LEFT JOIN interviews i ON i.id=s.interview_id WHERE s.id=?''', (slot_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='Slot not found')
        if row['withdrawn'] or row['interview_status'] == 'scheduled':
            raise HTTPException(status_code=409, detail='Slot is withdrawn or has a scheduled interview')
        if row['start_at'] <= utc_text(datetime.now(UTC)):
            raise HTTPException(status_code=409, detail='Cannot withdraw a past or in-progress slot')
        db.execute('UPDATE interview_slots SET withdrawn=1 WHERE id=?', (slot_id,))
        db.execute('INSERT INTO slot_audit_events (slot_id,actor_id,action) VALUES (?,?,?)',
                   (slot_id, user.id, 'slot_withdrawn'))
        return {**slot_view(row), 'withdrawn': True}

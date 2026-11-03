"""Human-managed, local-only interview slots with atomic candidate booking."""

import hashlib
import json
import secrets
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.auth import COOKIE_NAME, User, get_current_user, require_roles, token_digest
from app.db import connect
from app.scheduling import interview_view, utc_text
from app.slot_reservations import private_slot_conflict

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
        if 'target_application_id' not in columns:
            db.execute('ALTER TABLE interview_slots ADD COLUMN target_application_id INTEGER REFERENCES applications(id)')
        if 'target_candidate_id' not in columns:
            db.execute('ALTER TABLE interview_slots ADD COLUMN target_candidate_id INTEGER REFERENCES users(id)')
        db.execute('''CREATE TABLE IF NOT EXISTS slot_batches (
            id TEXT PRIMARY KEY, created_by INTEGER NOT NULL REFERENCES users(id),
            payload TEXT NOT NULL, fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','committed'))
        )''')


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


def candidate_available_slots(db, application_id: int, user: User, *, limit: int = -1) -> list[dict]:
    """Return the same owner-scoped slots used for booking, within the caller's snapshot."""
    application = owned_advanced_application(db, application_id, user)
    if db.execute("""SELECT 1 FROM interviews i WHERE i.application_id = ? AND i.status = 'scheduled'
                  AND NOT EXISTS (SELECT 1 FROM interview_completions c WHERE c.interview_id=i.id)""", (application_id,)).fetchone():
        return []
    now = utc_text(datetime.now(UTC))
    rows = db.execute('''SELECT s.* FROM interview_slots s
        LEFT JOIN interviews linked ON linked.id = s.interview_id
        WHERE s.withdrawn = 0 AND s.start_at > ? AND (s.interview_id IS NULL OR linked.status = 'cancelled')
        AND (s.target_application_id IS NULL OR (s.target_application_id=? AND s.target_candidate_id=?))
        AND NOT EXISTS (SELECT 1 FROM interviews i WHERE i.status = 'scheduled'
            AND i.start_at < s.end_at AND i.end_at > s.start_at
            AND (i.candidate_id = ? OR i.interviewer_key = s.interviewer_key))
        AND NOT EXISTS (SELECT 1 FROM interview_slots reserved
            WHERE reserved.target_application_id IS NOT NULL AND reserved.withdrawn=0 AND reserved.id != s.id
            AND reserved.start_at < s.end_at AND reserved.end_at > s.start_at
            AND (reserved.interviewer_key=s.interviewer_key OR reserved.target_candidate_id=?))
        ORDER BY s.start_at, s.id LIMIT ?''',
        (now, application_id, user.id, application['candidate_id'], user.id, limit)).fetchall()
    return [slot_view(row) for row in rows]


class BatchSlot(SlotCreate):
    model_config = ConfigDict(extra='forbid')
    application_id: int = Field(gt=0, strict=True)


class BatchPreview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    items: list[BatchSlot] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def unique_applications(self):
        if len({item.application_id for item in self.items}) != len(self.items):
            raise ValueError('Each application must be distinct')
        return self


class BatchCommit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')


def _require_current_role(db, request: Request, user: User, role: str) -> None:
    """Recheck the requesting session inside the serialized write transaction."""
    session = db.execute('''SELECT u.username, u.role FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.user_id=? AND s.token_hash=? AND s.expires_at>? AND u.active=1''',
        (user.id, token_digest(request.cookies.get(COOKIE_NAME, '')), int(time.time()))).fetchone()
    if session is None or session['username'] != user.username:
        raise HTTPException(status_code=401, detail='Authentication required')
    if session['role'] != role:
        raise HTTPException(status_code=403, detail=f'{role.capitalize()} access required')


def _validate_batch(db, items: list[BatchSlot]) -> dict[int, int]:
    now = datetime.now(UTC)
    candidates = {}
    for index, item in enumerate(items):
        application = db.execute('SELECT id,candidate_id,status FROM applications WHERE id=?', (item.application_id,)).fetchone()
        if application is None or application['status'] != 'advanced':
            raise HTTPException(status_code=409, detail='An application is not eligible')
        candidates[item.application_id] = application['candidate_id']
        if item.start_at <= now:
            raise HTTPException(status_code=409, detail='Batch slots must be in the future')
        if db.execute("""SELECT 1 FROM interviews i WHERE i.application_id=? AND i.status='scheduled'
                          AND NOT EXISTS (SELECT 1 FROM interview_completions c WHERE c.interview_id=i.id)""", (item.application_id,)).fetchone():
            raise HTTPException(status_code=409, detail='An application already has an interview')
        start, end, interviewer = utc_text(item.start_at), utc_text(item.end_at), item.interviewer.casefold()
        if db.execute("""SELECT 1 FROM interviews WHERE status='scheduled' AND start_at < ? AND end_at > ?
                          AND (candidate_id=? OR interviewer_key=?)""", (end, start, application['candidate_id'], interviewer)).fetchone():
            raise HTTPException(status_code=409, detail='Interview conflict')
        if db.execute('''SELECT 1 FROM interview_slots s LEFT JOIN applications a ON a.id=s.target_application_id
                          WHERE s.withdrawn=0 AND s.start_at < ? AND s.end_at > ?
                          AND (s.interviewer_key=? OR a.candidate_id=?)''', (end, start, interviewer, application['candidate_id'])).fetchone():
            raise HTTPException(status_code=409, detail='Slot conflict')
        for other in items[:index]:
            if (item.start_at < other.end_at and item.end_at > other.start_at and
                    (item.interviewer.casefold() == other.interviewer.casefold() or
                     application['candidate_id'] == candidates[other.application_id])):
                raise HTTPException(status_code=409, detail='Batch candidate or interviewer conflict')
    return candidates


@router.post('/slots/batches/preview', status_code=201)
def preview_batch(payload: BatchPreview, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        candidates = _validate_batch(db, payload.items)
        proposal_id = secrets.token_hex(16)
        items = [item.model_dump(mode='json') for item in payload.items]
        identities = [{'application_id': item.application_id, 'candidate_id': candidates[item.application_id]}
                      for item in payload.items]
        encoded = json.dumps({'id': proposal_id, 'actor': user.id, 'items': items, 'identities': identities}, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        db.execute("INSERT INTO slot_batches(id,created_by,payload,fingerprint,status) VALUES (?,?,?,?,'pending')",
                   (proposal_id, user.id, encoded, fingerprint))
        _require_current_role(db, request, user, 'hr')
        return {'proposal_id': proposal_id, 'fingerprint': fingerprint,
                'items': [{**item, 'candidate_id': identity['candidate_id']} for item, identity in zip(items, identities, strict=True)]}


@router.post('/slots/batches/{proposal_id}/commit')
def commit_batch(proposal_id: str, payload: BatchCommit, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        row = db.execute('SELECT * FROM slot_batches WHERE id=? AND created_by=?', (proposal_id, user.id)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='Batch not found')
        if row['status'] != 'pending' or not secrets.compare_digest(row['fingerprint'], payload.fingerprint):
            raise HTTPException(status_code=409, detail='Batch changed or already committed')
        if hashlib.sha256(row['payload'].encode()).hexdigest() != row['fingerprint']:
            raise HTTPException(status_code=409, detail='Batch integrity error')
        data = json.loads(row['payload'])
        if data.get('id') != proposal_id or data.get('actor') != user.id:
            raise HTTPException(status_code=409, detail='Batch integrity error')
        items = BatchPreview.model_validate({'items': data['items']}).items
        candidates = _validate_batch(db, items)
        identities = [{'application_id': item.application_id, 'candidate_id': candidates[item.application_id]}
                      for item in items]
        if data.get('identities') != identities:
            raise HTTPException(status_code=409, detail='Candidate ownership changed; review a new batch')
        slots = []
        for item in items:
            created = create_slot_record(db, item, user.id)
            db.execute('UPDATE interview_slots SET target_application_id=?, target_candidate_id=? WHERE id=?',
                       (item.application_id, candidates[item.application_id], created['id']))
            slots.append({**created, 'application_id': item.application_id})
        db.execute("UPDATE slot_batches SET status='committed' WHERE id=?", (proposal_id,))
        _require_current_role(db, request, user, 'hr')
        return {'slots': slots}


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
    if private_slot_conflict(db, utc_text(payload.start_at), utc_text(payload.end_at), payload.interviewer.casefold()):
        raise HTTPException(status_code=409, detail='Interviewer has a reserved private slot')
    cursor = db.execute(
        'INSERT INTO interview_slots (start_at,end_at,interviewer,interviewer_key,created_by) VALUES (?,?,?,?,?)',
        (utc_text(payload.start_at), utc_text(payload.end_at), payload.interviewer,
         payload.interviewer.casefold(), actor_id),
    )
    db.execute('INSERT INTO slot_audit_events (slot_id,actor_id,action) VALUES (?, ?, ?)',
               (cursor.lastrowid, actor_id, 'slot_created'))
    return slot_view(db.execute('SELECT * FROM interview_slots WHERE id = ?', (cursor.lastrowid,)).fetchone())


@router.get('/slots')
def list_slots(request: Request, user: Authenticated, application_id: int | None = None,
               target_application_id: int | None = None):
    with connect(request.app.state.database_path) as db:
        if user.role == 'hr':
            db.execute('BEGIN')
            _require_current_role(db, request, user, 'hr')
            if target_application_id is not None and target_application_id <= 0:
                raise HTTPException(status_code=422, detail='Application is invalid')
            clause = 'WHERE s.target_application_id=?' if target_application_id is not None else ''
            params = (target_application_id,) if target_application_id is not None else ()
            rows = db.execute(f'''SELECT s.*, i.status AS interview_status
                FROM interview_slots s LEFT JOIN interviews i ON i.id = s.interview_id
                {clause} ORDER BY s.start_at, s.id''', params).fetchall()
            _require_current_role(db, request, user, 'hr')
            return [{**slot_view(row), 'target_application_id': row['target_application_id'], 'booked': row['interview_status'] == 'scheduled',
                     'withdrawn': bool(row['withdrawn'])} for row in rows]
        if user.role != 'candidate' or target_application_id is not None:
            raise HTTPException(status_code=403, detail='Access denied')
        if application_id is None or application_id <= 0:
            raise HTTPException(status_code=422, detail='Application is required')
        return candidate_available_slots(db, application_id, user)


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
        if slot['target_application_id'] is not None and (slot['target_application_id'] != application['id']
                                                           or slot['target_candidate_id'] != user.id):
            raise HTTPException(status_code=404, detail='Slot not found')
        now = utc_text(datetime.now(UTC))
        if slot['withdrawn'] or slot['start_at'] <= now or slot['interview_status'] == 'scheduled':
            raise HTTPException(status_code=409, detail='Slot is not available')
        if private_slot_conflict(db, slot['start_at'], slot['end_at'], slot['interviewer_key'],
                                 candidate_id=user.id, exclude_slot_id=slot_id):
            raise HTTPException(status_code=409, detail='Candidate or interviewer has a reserved private slot')
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
        result = interview_view(db.execute('SELECT * FROM interviews WHERE id = ?', (cursor.lastrowid,)).fetchone())
        _require_current_role(db, request, user, 'candidate')
        return result


@router.post('/slots/{slot_id}/withdraw')
def withdraw_slot(slot_id: int, request: Request, user: HR,
                  expected_application_id: int | None = Query(default=None, gt=0)):
    """Retire an unbooked slot without cancelling an existing interview."""
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        row = db.execute('''SELECT s.*, i.status AS interview_status FROM interview_slots s
            LEFT JOIN interviews i ON i.id=s.interview_id WHERE s.id=?''', (slot_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='Slot not found')
        if expected_application_id is not None:
            application = db.execute('SELECT candidate_id,status FROM applications WHERE id=?',
                                     (expected_application_id,)).fetchone()
            if (row['target_application_id'] != expected_application_id or application is None
                    or application['status'] != 'advanced' or row['target_candidate_id'] != application['candidate_id']):
                raise HTTPException(status_code=409, detail='Slot or application changed; reload before withdrawing')
        if row['withdrawn'] or row['interview_status'] == 'scheduled':
            raise HTTPException(status_code=409, detail='Slot is withdrawn or has a scheduled interview')
        if row['start_at'] <= utc_text(datetime.now(UTC)):
            raise HTTPException(status_code=409, detail='Cannot withdraw a past or in-progress slot')
        db.execute('UPDATE interview_slots SET withdrawn=1 WHERE id=?', (slot_id,))
        db.execute('INSERT INTO slot_audit_events (slot_id,actor_id,action) VALUES (?,?,?)',
                   (slot_id, user.id, 'slot_withdrawn'))
        _require_current_role(db, request, user, 'hr')
        return {**slot_view(row), 'withdrawn': True}

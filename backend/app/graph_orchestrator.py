"""A local, human-supervised LangGraph review with explicit slot approval.

Each review belongs to the authenticated HR user who started it. SQLite
checkpoints contain the review results (including potentially sensitive CV
evidence), so the database must be protected like the recruitment database.
The graph can invoke a bounded read-only model agent when explicitly enabled;
it never sends mail or makes a hiring decision.
Policy retrieval saves source identifiers and digests only. Answers are rebuilt
from currently authorized sources on every read; questions and excerpts are not
stored in checkpoints.
Old checkpoint payloads are redacted from responses, but may still exist on disk.
"""

import errno
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from threading import BoundedSemaphore
from typing import Annotated, Literal, TypedDict

from fastapi import APIRouter, Depends, HTTPException, Request
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field, StrictBool, model_validator

from app.adaptive_planner import context_for, make_plan
from app.agent import AgentError, run_agent
from app.auth import COOKIE_NAME, User, get_current_user, require_roles, token_digest
from app.conversations import _authorized_sources, _digest
from app.db import connect
from app.google_outbox import cancellation_block_reason
from app.graph_semantic import context as semantic_context
from app.graph_semantic import restore_references, save_references
from app.interview_guide import guide_from_matching
from app.matching_specialist import matching_snapshot
from app.policy_agent import (
    NUMERIC_NOTICE,
    POLICY_CONTACT_HR,
    _numeric_notice,
    authorized_policy_sources,
    run_policy_agent,
)
from app.provisional_queue import (
    _assignment_revision,
    _bounded_job,
    _job_revision,
    _source_revisions,
    provisional_application_state,
)
from app.recruitment import (
    _require_current_role,
    get_application,
    normalize,
    record_hire,
    record_offer,
    record_rejection,
    score_masked_resume,
    store_screening,
    validate_hire_eligibility,
    validate_offer_eligibility,
)
from app.resume_fields import resume_fields_for_display
from app.resume_mask import masked_source
from app.resume_structure import structure_resume
from app.scheduling import record_interview_completion, utc_text, validate_interview_completion
from app.slot_reservations import private_slot_conflict
from app.slots import BatchSlot, SlotCreate, _validate_batch, create_slot_record
from app.workflow import candidate_stage

router = APIRouter(prefix="/api/graph-reviews")
HR = Annotated[User, Depends(require_roles("hr"))]
logger = logging.getLogger(__name__)
_matching_workers = BoundedSemaphore(2)
# Graph matching recomputes a score within the atomic approval transaction.
# Keep its budget tighter than direct screening while supporting longer CVs.
MAX_GRAPH_SOURCE_CHARS = 12_000
MAX_GRAPH_SOURCE_BYTES = MAX_GRAPH_SOURCE_CHARS * 4
MAX_GRAPH_MASKED_CHARS = 16_000


class ReviewRequest(BaseModel):
    application_id: int = Field(gt=0)
    policy_question: str = Field(default="", max_length=2000)
    intent: Literal['full', 'policy', 'scheduling', 'status', 'create_slot', 'create_private_slot', 'advance_application', 'advance_and_reserve_slot', 'semantic_review', 'semantic_advance_and_reserve_slot', 'adaptive', 'screen_application', 'complete_interview', 'present_offer', 'finalize_hire', 'reject_application'] = 'full'
    slot: SlotCreate | None = None
    interview_id: int | None = Field(default=None, gt=0)
    offer_details: str | None = Field(default=None, max_length=4000)
    rejection_reason: str | None = Field(default=None, max_length=1000)
    adaptive_goal: Literal['review_application', 'schedule_interview', 'advance_and_reserve'] | None = None
    allow_semantic: StrictBool = False

    @model_validator(mode='before')
    @classmethod
    def restrict_adaptive_input(cls, data):
        if (isinstance(data, dict) and data.get('intent') == 'adaptive' and
                set(data) - {'application_id', 'intent', 'adaptive_goal', 'slot', 'allow_semantic', 'policy_question'}):
            raise ValueError('Adaptive review accepts no tools, identity overrides, or extra fields')
        return data

    @model_validator(mode='after')
    def check_policy_question(self):
        if (self.intent == 'reject_application') != bool(self.rejection_reason and self.rejection_reason.strip()):
            raise ValueError('Rejection reason is required only for rejection review')
        if (self.intent == 'present_offer') != bool(self.offer_details and self.offer_details.strip()):
            raise ValueError('Offer details are required only for offer review')
        if (self.intent == 'complete_interview') != (self.interview_id is not None):
            raise ValueError('Interview ID is required only for completion review')
        if self.intent == 'policy' and not self.policy_question.strip():
            raise ValueError('Policy review requires a question')
        if self.intent in ('scheduling', 'status', 'advance_application', 'advance_and_reserve_slot', 'semantic_review', 'semantic_advance_and_reserve_slot', 'screen_application', 'complete_interview', 'present_offer', 'finalize_hire', 'reject_application') and self.policy_question.strip():
            raise ValueError('Policy question is not used by this review')
        needs_slot = self.intent in ('create_slot', 'create_private_slot', 'advance_and_reserve_slot', 'semantic_advance_and_reserve_slot') or (self.intent == 'adaptive' and self.adaptive_goal in ('schedule_interview', 'advance_and_reserve'))
        if needs_slot != (self.slot is not None):
            raise ValueError('Slot details are required only for scheduling')
        if self.intent in ('create_slot', 'create_private_slot') and self.policy_question.strip():
            raise ValueError('Policy question is not used by this review')
        if (self.intent == 'adaptive') != (self.adaptive_goal is not None):
            raise ValueError('Adaptive goal is required only for adaptive review')
        if self.allow_semantic and self.intent != 'adaptive':
            raise ValueError('Semantic planning is available only for adaptive review')
        return self


class ReviewReply(BaseModel):
    reviewed: bool
    approval_fingerprint: str | None = Field(default=None, max_length=64)


class ReviewState(TypedDict, total=False):
    application_id: int
    interview_id: int
    offer_draft_id: str
    rejection_draft_id: str
    intent: str
    allow_semantic: bool
    plan: list[str]
    parsed: dict
    parser_source_ref: str
    parser_context_ref: str
    matching: dict
    matching_specialist_ref: str
    matching_unavailable_ref: dict
    provisional_queue_ref: int
    policy: dict
    scheduling: dict
    status: dict
    agent_review: dict
    reviewed: bool
    slot: dict
    operation_id: str
    action_proposal: dict
    action_result: dict
    adaptive_goal: str
    plan_source: str
    plan_context: dict
    context_digest: str
    assessment_revision: int
    semantic_refs: dict
    screen_revision: str
    screen_score_digest: str


def _bounded_matching_inputs(db, application_id: int):
    """Read bounded, masked scoring inputs from the caller's database snapshot."""
    row = db.execute("SELECT job_id,length(CAST(resume_text AS BLOB)) AS source_bytes FROM applications WHERE id=?",
                     (application_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail='Application not found')
    too_large = {'code': 'graph_matching_work_limit',
                 'message': 'Graph matching supports resumes up to 12,000 characters, bounded redacted text and at most 20 job skills of 80 characters each. Review larger inputs manually.'}
    if row['source_bytes'] > MAX_GRAPH_SOURCE_BYTES:
        raise HTTPException(status_code=422, detail=too_large)
    raw = db.execute('SELECT resume_text FROM applications WHERE id=?', (application_id,)).fetchone()[0]
    if len(raw) > MAX_GRAPH_SOURCE_CHARS:
        raise HTTPException(status_code=422, detail=too_large)
    try:
        job = _bounded_job(db, row['job_id'])
    except HTTPException as exc:
        if exc.status_code == 422:
            raise HTTPException(status_code=422, detail=too_large) from exc
        raise
    source = masked_source(db, application_id)
    if (len(source['text']) > MAX_GRAPH_MASKED_CHARS or
            len(normalize(source['text'])) > MAX_GRAPH_MASKED_CHARS):
        raise HTTPException(status_code=422, detail=too_large)
    return row['job_id'], job, source


def _screen_snapshot(db, application_id: int):
    """Build an ephemeral score and references from one database snapshot."""
    application = get_application(db, application_id)
    _, job, source = _bounded_matching_inputs(db, application_id)
    _, context_digest = context_for(db, application_id)
    row = db.execute('SELECT revision FROM rubric_publication_revisions WHERE application_id=?', (application_id,)).fetchone()
    state_row = db.execute('SELECT revision FROM graph_screen_revisions WHERE application_id=?', (application_id,)).fetchone()
    revision = f"{row['revision'] if row else 0}:{state_row['revision'] if state_row else 0}"
    score = score_masked_resume(job, source)
    encoded = json.dumps(score, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    score_digest = hashlib.sha256(encoded).hexdigest()
    return application, score, context_digest, revision, score_digest


def _screen_is_current(state, application, context_digest, revision, score_digest):
    return (application['status'] == 'submitted' and not application['score_json'] and
            application['candidate_id'] == state.get('action_proposal', {}).get('candidate_id') and
            context_digest == state.get('context_digest') and revision == state.get('screen_revision') and
            score_digest == state.get('screen_score_digest'))


class _AtomicResumeSaver(SqliteSaver):
    """Keep LangGraph's writes in the caller's single resume transaction."""

    @contextmanager
    def cursor(self, transaction: bool = True):
        # setup() must run before BEGIN: its executescript commits transactions.
        with self.lock:
            if not self.is_setup or not self.conn.in_transaction:
                raise RuntimeError('Resume transaction is not active')
            cursor = self.conn.cursor()
            try:
                yield cursor
            finally:
                cursor.close()


def _authorize_resume(db, request: Request, user: User, thread_id: str) -> None:
    token = request.cookies.get(COOKIE_NAME, '')
    actor = db.execute('''SELECT u.id, u.username, u.role, u.active FROM sessions s
        JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.user_id=? AND s.expires_at>?''',
        (token_digest(token), user.id, int(time.time()))).fetchone()
    if actor is None or not actor[3] or actor[1] != user.username:
        raise HTTPException(status_code=401, detail='Authentication required')
    if actor[2] != 'hr':
        raise HTTPException(status_code=403, detail='HR access required')
    owner = db.execute('SELECT 1 FROM graph_review_owners WHERE thread_id=? AND owner_id=?',
                       (thread_id, user.id)).fetchone()
    if owner is None:
        raise HTTPException(status_code=404, detail='Review not found')


def initialize_graph_reviews(path: str | Path) -> None:
    with connect(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS graph_review_owners (
            thread_id TEXT PRIMARY KEY,
            application_id INTEGER NOT NULL REFERENCES applications(id),
            owner_id INTEGER NOT NULL REFERENCES users(id)
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS graph_screen_revisions (
            application_id INTEGER PRIMARY KEY REFERENCES applications(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL DEFAULT 0
        )""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS graph_screen_revision_state_changed
            AFTER UPDATE OF status,score_json ON applications BEGIN
                INSERT OR IGNORE INTO graph_screen_revisions(application_id,revision)
                    SELECT id,0 FROM applications WHERE id=OLD.id;
                UPDATE graph_screen_revisions SET revision=revision+1 WHERE application_id=OLD.id;
            END""")

        db.execute("""CREATE TABLE IF NOT EXISTS graph_interview_revisions (
            interview_id INTEGER PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0
        )""")
        for event, prefix in (('UPDATE', 'NEW'), ('DELETE', 'OLD'), ('INSERT', 'NEW')):
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS graph_interview_revision_{event.lower()}
                AFTER {event} ON interviews BEGIN
                    INSERT OR IGNORE INTO graph_interview_revisions(interview_id,revision) VALUES({prefix}.id,0);
                    UPDATE graph_interview_revisions SET revision=revision+1 WHERE interview_id={prefix}.id;
                END""")


        db.execute("""CREATE TABLE IF NOT EXISTS graph_offer_drafts (
            thread_id TEXT PRIMARY KEY REFERENCES graph_review_owners(thread_id) ON DELETE CASCADE,
            application_id INTEGER NOT NULL REFERENCES applications(id),
            owner_id INTEGER NOT NULL REFERENCES users(id), details TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS graph_offer_revisions (
            application_id INTEGER PRIMARY KEY REFERENCES applications(id), revision INTEGER NOT NULL DEFAULT 0
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS graph_rejection_drafts (
            thread_id TEXT PRIMARY KEY REFERENCES graph_review_owners(thread_id) ON DELETE CASCADE,
            application_id INTEGER NOT NULL REFERENCES applications(id),
            owner_id INTEGER NOT NULL REFERENCES users(id), reason TEXT NOT NULL,
            expires_at INTEGER NOT NULL
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS graph_rejection_revisions (
            application_id INTEGER PRIMARY KEY REFERENCES applications(id), revision INTEGER NOT NULL DEFAULT 0
        )""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS graph_rejection_draft_update
            AFTER UPDATE ON graph_rejection_drafts BEGIN
            INSERT OR IGNORE INTO graph_rejection_revisions(application_id,revision)
                SELECT id,0 FROM applications WHERE id IN (NEW.application_id,OLD.application_id);
            UPDATE graph_rejection_revisions SET revision=revision+1
                WHERE application_id IN (NEW.application_id,OLD.application_id);
        END""")
        for table, column in (('interview_slots', 'target_application_id'), ('google_deliveries', 'application_id')):
            for event, alias in (('INSERT', 'NEW'), ('UPDATE', 'NEW'), ('DELETE', 'OLD')):
                old_id = 'OLD.' + column if event == 'UPDATE' else alias + '.' + column
                new_id = alias + '.' + column
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS graph_rejection_{table}_{event.lower()}
                    AFTER {event} ON {table} BEGIN
                    INSERT OR IGNORE INTO graph_rejection_revisions(application_id,revision)
                        SELECT id,0 FROM applications WHERE id IN ({new_id},{old_id});
                    UPDATE graph_rejection_revisions SET revision=revision+1
                        WHERE application_id IN ({new_id},{old_id});
                END""")
        for event, alias in (('INSERT', 'NEW'), ('UPDATE', 'NEW'), ('DELETE', 'OLD')):
            old_id = 'OLD.delivery_id' if event == 'UPDATE' else alias + '.delivery_id'
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS graph_rejection_removal_{event.lower()}
                AFTER {event} ON google_calendar_removals BEGIN
                INSERT OR IGNORE INTO graph_rejection_revisions(application_id,revision)
                    SELECT application_id,0 FROM google_deliveries WHERE id IN ({alias}.delivery_id,{old_id});
                UPDATE graph_rejection_revisions SET revision=revision+1 WHERE application_id IN
                    (SELECT application_id FROM google_deliveries WHERE id IN ({alias}.delivery_id,{old_id}));
            END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS graph_offer_candidate_access_update
            AFTER UPDATE OF role, active ON users
            WHEN OLD.role IS NOT NEW.role OR OLD.active IS NOT NEW.active BEGIN
            INSERT OR IGNORE INTO graph_offer_revisions(application_id,revision)
                SELECT id,0 FROM applications WHERE candidate_id=NEW.id;
            UPDATE graph_offer_revisions SET revision=revision+1 WHERE application_id IN
                (SELECT id FROM applications WHERE candidate_id=NEW.id);
        END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS graph_offer_candidate_identity_update
            AFTER UPDATE OF username, display_name ON users
            WHEN OLD.username IS NOT NEW.username OR OLD.display_name IS NOT NEW.display_name BEGIN
            INSERT OR IGNORE INTO graph_offer_revisions(application_id,revision)
                SELECT id,0 FROM applications WHERE candidate_id=NEW.id;
            UPDATE graph_offer_revisions SET revision=revision+1 WHERE application_id IN
                (SELECT id FROM applications WHERE candidate_id=NEW.id);
        END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS graph_offer_job_title_update
            AFTER UPDATE OF title ON jobs WHEN OLD.title IS NOT NEW.title BEGIN
            INSERT OR IGNORE INTO graph_offer_revisions(application_id,revision)
                SELECT id,0 FROM applications WHERE job_id=NEW.id;
            UPDATE graph_offer_revisions SET revision=revision+1 WHERE application_id IN
                (SELECT id FROM applications WHERE job_id=NEW.id);
        END""")
        for table in ('applications', 'interviews', 'job_offers'):
            for event, alias in (('INSERT','NEW'), ('UPDATE','NEW'), ('DELETE','OLD')):
                if table == 'applications':
                    if event != 'UPDATE':
                        continue
                    columns = ' OF status,candidate_id,job_id'
                    identifier = alias + '.id'
                else:
                    columns = ''
                    identifier = alias + '.application_id'
                old_identifier = 'OLD.application_id' if event == 'UPDATE' and table != 'applications' else identifier
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS graph_offer_{table}_{event.lower()}
                    AFTER {event}{columns} ON {table} BEGIN
                    INSERT OR IGNORE INTO graph_offer_revisions(application_id,revision)
                        SELECT id,0 FROM applications WHERE id IN ({identifier},{old_identifier});
                    UPDATE graph_offer_revisions SET revision=revision+1 WHERE application_id IN ({identifier},{old_identifier});
                END""")
        for event, alias in (('INSERT','NEW'), ('UPDATE','NEW'), ('DELETE','OLD')):
            old_interview = 'OLD.interview_id' if event == 'UPDATE' else alias + '.interview_id'
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS graph_offer_completion_{event.lower()}
                AFTER {event} ON interview_completions BEGIN
                INSERT OR IGNORE INTO graph_offer_revisions(application_id,revision)
                    SELECT application_id,0 FROM interviews WHERE id IN ({alias}.interview_id,{old_interview});
                UPDATE graph_offer_revisions SET revision=revision+1 WHERE application_id IN
                    (SELECT application_id FROM interviews WHERE id IN ({alias}.interview_id,{old_interview}));
            END""")


def _rejection_snapshot(db, application_id: int, draft_id: str, owner_id: int):
    draft = db.execute('SELECT * FROM graph_rejection_drafts WHERE thread_id=? AND application_id=? AND owner_id=?',
                       (draft_id, application_id, owner_id)).fetchone()
    if draft is None or draft['expires_at'] <= int(time.time()):
        raise HTTPException(status_code=409, detail='Rejection draft unavailable or expired; start a new review')
    application = get_application(db, application_id)
    if application['status'] not in ('submitted', 'advanced', 'offered'):
        raise HTTPException(status_code=409, detail='Application cannot be rejected from its current status')
    candidate = db.execute('SELECT username,display_name FROM users WHERE id=?', (application['candidate_id'],)).fetchone()
    job = db.execute('SELECT title FROM jobs WHERE id=?', (application['job_id'],)).fetchone()
    if candidate is None or job is None:
        raise HTTPException(status_code=409, detail='Candidate or job unavailable')
    now = utc_text(datetime.now(UTC))
    interviews = db.execute("""SELECT id,start_at,end_at,interviewer FROM interviews
        WHERE application_id=? AND status='scheduled' AND start_at>? ORDER BY id LIMIT 51""",
        (application_id, now)).fetchall()
    if len(interviews) > 50:
        raise HTTPException(status_code=409, detail='Too many interviews; review manually')
    if any(cancellation_block_reason(db, row['id']) for row in interviews):
        raise HTTPException(status_code=409, detail='Resolve the Google delivery before rejecting this application')
    slots = db.execute("""SELECT s.id,s.start_at,s.end_at,s.interviewer FROM interview_slots s
        LEFT JOIN interviews i ON i.id=s.interview_id WHERE s.target_application_id=? AND s.withdrawn=0
        AND s.start_at>? AND (s.interview_id IS NULL OR i.status='cancelled' OR
        (i.status='scheduled' AND i.application_id=? AND i.start_at>?)) ORDER BY s.id LIMIT 51""",
        (application_id, now, application_id, now)).fetchall()
    if len(slots) > 50:
        raise HTTPException(status_code=409, detail='Too many private slots; review manually')
    offer = db.execute("SELECT id,status FROM job_offers WHERE application_id=? AND status IN ('presented','accepted')", (application_id,)).fetchone()
    identity = {'candidate_id': application['candidate_id'], 'candidate_name': candidate['display_name'],
                'candidate_username': candidate['username'], 'job_id': application['job_id'], 'job_title': job['title']}
    revisions = [db.execute(f'SELECT revision FROM {table} WHERE application_id=?', (application_id,)).fetchone()
                 for table in ('graph_offer_revisions', 'graph_rejection_revisions')]
    effects = {'interviews': [dict(row) for row in interviews], 'slots': [dict(row) for row in slots],
               'offer_id': offer['id'] if offer else None, 'offer_status': offer['status'] if offer else None}
    data = {'application_id': application_id, **identity, 'application_status': application['status'],
            'reason_sha256': hashlib.sha256(draft['reason'].encode()).hexdigest(), 'effects': effects,
            'revisions': [row['revision'] if row else 0 for row in revisions], 'expires_at': draft['expires_at']}
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return draft['reason'], identity, effects, application['status'], fingerprint


def _check_rejection_effects(db, effects):
    """Fail and roll back if time passed or the recorded effects differ from the review."""
    now = utc_text(datetime.now(UTC))
    for interview in effects['interviews']:
        row = db.execute('SELECT status FROM interviews WHERE id=?', (interview['id'],)).fetchone()
        if interview['start_at'] <= now or row is None or row['status'] != 'cancelled':
            raise HTTPException(status_code=409, detail='Interview timing changed; start a new review')
    for slot in effects['slots']:
        row = db.execute('SELECT withdrawn FROM interview_slots WHERE id=?', (slot['id'],)).fetchone()
        if slot['start_at'] <= now or row is None or row['withdrawn'] != 1:
            raise HTTPException(status_code=409, detail='Slot timing changed; start a new review')
    if effects['offer_id'] is not None:
        offer = db.execute('SELECT status FROM job_offers WHERE id=?', (effects['offer_id'],)).fetchone()
        expected = 'withdrawn' if effects['offer_status'] == 'presented' else 'accepted'
        if offer is None or offer['status'] != expected:
            raise HTTPException(status_code=409, detail='Offer changed; start a new review')


def _offer_snapshot(db, application_id: int, draft_id: str, owner_id: int):
    draft = db.execute('SELECT * FROM graph_offer_drafts WHERE thread_id=? AND application_id=? AND owner_id=?',
                       (draft_id, application_id, owner_id)).fetchone()
    if draft is None or draft['expires_at'] <= int(time.time()):
        raise HTTPException(status_code=409, detail='Offer draft unavailable or expired; start a new review')
    application = validate_offer_eligibility(db, application_id)
    candidate = db.execute('SELECT username,display_name FROM users WHERE id=?', (application['candidate_id'],)).fetchone()
    job = db.execute('SELECT title FROM jobs WHERE id=?', (application['job_id'],)).fetchone()
    if candidate is None or job is None:
        raise HTTPException(status_code=409, detail='Candidate or job unavailable; start a new review')
    identity = {'candidate_id': application['candidate_id'], 'candidate_name': candidate['display_name'],
                'candidate_username': candidate['username'], 'job_id': application['job_id'], 'job_title': job['title']}
    revision = db.execute('SELECT revision FROM graph_offer_revisions WHERE application_id=?', (application_id,)).fetchone()
    data = {'application_id': application_id, **identity, 'status': application['status'],
            'offer_revision': revision['revision'] if revision else 0,
            'details_sha256': hashlib.sha256(draft['details'].encode('utf-8')).hexdigest(),
            'expires_at': draft['expires_at']}
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return draft['details'], identity, digest


def _hire_snapshot(db, application_id: int):
    application, offer = validate_hire_eligibility(db, application_id)
    candidate = db.execute('SELECT username,display_name FROM users WHERE id=?', (application['candidate_id'],)).fetchone()
    job = db.execute('SELECT title FROM jobs WHERE id=?', (application['job_id'],)).fetchone()
    if candidate is None or job is None:
        raise HTTPException(status_code=409, detail='Candidate or job unavailable')
    identity = {'candidate_id': application['candidate_id'], 'candidate_name': candidate['display_name'],
                'candidate_username': candidate['username'], 'job_id': application['job_id'], 'job_title': job['title']}
    revision = db.execute('SELECT revision FROM graph_offer_revisions WHERE application_id=?', (application_id,)).fetchone()
    data = {'application_id': application_id, **identity, 'status': application['status'],
            'offer_id': offer['id'], 'offer_status': offer['status'], 'responded_at': offer['responded_at'],
            'offer_revision': revision['revision'] if revision else 0,
            'details_sha256': hashlib.sha256(offer['details'].encode('utf-8')).hexdigest()}
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return offer, identity, digest


def _interview_completion_snapshot(db, application_id: int, interview_id: int):
    """Bind completion approval to the exact interview and monotonic revisions."""
    row = validate_interview_completion(db, interview_id)
    if row['application_id'] != application_id or row['candidate_id'] != row['application_candidate_id']:
        raise HTTPException(status_code=409, detail='Interview does not belong to this application and candidate')
    revision = db.execute('SELECT revision FROM graph_interview_revisions WHERE interview_id=?', (interview_id,)).fetchone()
    app_revision = db.execute('SELECT revision FROM rubric_publication_revisions WHERE application_id=?', (application_id,)).fetchone()
    status_revision = db.execute('SELECT revision FROM graph_screen_revisions WHERE application_id=?', (application_id,)).fetchone()
    values = {key: row[key] for key in ('id', 'application_id', 'candidate_id', 'start_at', 'end_at', 'interviewer', 'status', 'application_status')}
    values.update(interview_revision=revision['revision'] if revision else 0,
                  application_revision=app_revision['revision'] if app_revision else 0,
                  status_revision=status_revision['revision'] if status_revision else 0)
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return row, digest


def _current_scheduling(path: str | Path, application_id: int) -> dict:
    with connect(path) as db:
        application = get_application(db, application_id)
        if application["status"] != "advanced":
            return {"available_slots": [], "eligible": False,
                    "notice": "Application is not eligible for candidate booking."}
        completed = bool(db.execute("""SELECT 1 FROM interviews i JOIN interview_completions c
                ON c.interview_id=i.id WHERE i.application_id=? LIMIT 1""", (application_id,)).fetchone())
        active = db.execute("""SELECT 1 FROM interviews i WHERE i.application_id=? AND i.status='scheduled'
                AND NOT EXISTS (SELECT 1 FROM interview_completions c WHERE c.interview_id=i.id) LIMIT 1""",
                (application_id,)).fetchone()
        if active:
            return {"available_slots": [], "eligible": False, "interview_completed": completed,
                    "notice": "Application already has a scheduled interview."}
        rows = db.execute("""SELECT s.id, s.start_at, s.end_at FROM interview_slots s
            LEFT JOIN interviews linked ON linked.id = s.interview_id
            WHERE s.withdrawn = 0 AND s.start_at > ? AND (s.interview_id IS NULL OR linked.status = 'cancelled')
            AND (s.target_application_id IS NULL OR (s.target_application_id=? AND s.target_candidate_id=?))
            AND NOT EXISTS (SELECT 1 FROM interviews i WHERE i.status = 'scheduled'
                AND i.start_at < s.end_at AND i.end_at > s.start_at
                AND (i.candidate_id = ? OR i.interviewer_key = s.interviewer_key)
                AND NOT EXISTS (SELECT 1 FROM interview_completions c WHERE c.interview_id=i.id))
            AND NOT EXISTS (SELECT 1 FROM interview_slots reserved
                WHERE reserved.target_application_id IS NOT NULL AND reserved.withdrawn=0 AND reserved.id != s.id
                AND reserved.start_at < s.end_at AND reserved.end_at > s.start_at
                AND (reserved.interviewer_key=s.interviewer_key OR reserved.target_candidate_id=?))
            ORDER BY s.start_at, s.id LIMIT 10""",
            (utc_text(datetime.now(UTC)), application_id, application["candidate_id"], application["candidate_id"], application["candidate_id"])).fetchall()
    return {"available_slots": [dict(r) for r in rows], "eligible": True, "interview_completed": completed,
            "notice": ("A previous interview was completed. Availability is a snapshot; booking remains a separate action."
                       if completed else "Availability is a snapshot; booking remains a separate action.")}


def build_review_graph(path: str | Path, checkpointer, *, policy_question: str = "",
                       user: User | None = None, authorize=None, enable_agent: bool = False,
                       action_db=None, request: Request | None = None):
    """Compile a bounded graph around supervised domain operations."""
    def plan(state: ReviewState):
        if state.get('intent') == 'adaptive':
            if authorize is None or user is None:
                raise HTTPException(status_code=403, detail='HR access required')
            authorize()
            with connect(path) as db:
                facts, digest = context_for(db, state['application_id'])
            if state.get('allow_semantic'):
                facts['semantic_available'] = (os.environ.get('TAWZEEF_ENABLE_SEMANTIC_SCREENING') == '1'
                                               and bool(os.environ.get('TAWZEEF_EMBEDDING_MODEL_DIR', '').strip()))
            if state.get('adaptive_goal') == 'advance_and_reserve':
                if facts['status'] != 'submitted':
                    raise HTTPException(status_code=409, detail='Application must be submitted for advancement')
                facts['goal'] = 'advance_and_reserve'
            if state.get('adaptive_goal') == 'schedule_interview' and facts['status'] != 'advanced':
                raise HTTPException(status_code=409, detail='Application is not eligible for candidate booking')
            steps, source = make_plan(facts, enabled=enable_agent)
            if state.get('adaptive_goal') == 'schedule_interview' and 'scheduler' not in steps:
                steps = [*steps, 'scheduler']
            # Policy lookup is explicitly requested by HR, not selected by the
            # model planner. The question stays outside graph state/checkpoints.
            if policy_question.strip():
                steps = [*steps, 'policy']
            authorize()
            with connect(path) as db:
                if context_for(db, state['application_id'])[1] != digest:
                    raise HTTPException(status_code=409, detail='Application changed; start a new review')
            action_steps = (['prepare_slot', 'human_review', 'execute_slot'] if state.get('adaptive_goal') == 'schedule_interview'
                            else ['prepare_advance', 'human_review', 'execute_advance'] if state.get('adaptive_goal') == 'advance_and_reserve'
                            else ['human_review'])
            return {'plan': [*steps, *action_steps], 'plan_source': source,
                    'plan_context': facts, 'context_digest': digest}
        plans = {
            'full': ['parser', 'matching', 'policy', 'scheduler', 'status', 'agent_review', 'human_review'],
            'policy': ['policy', 'human_review'],
            'scheduling': ['scheduler', 'human_review'],
            'status': ['status', 'human_review'],
            'complete_interview': ['status', 'prepare_completion', 'human_review', 'execute_completion'],
            'present_offer': ['status', 'prepare_offer', 'human_review', 'execute_offer'],
            'finalize_hire': ['status', 'prepare_hire', 'human_review', 'execute_hire'],
            'reject_application': ['status', 'prepare_rejection', 'human_review', 'execute_rejection'],
            'create_slot': ['prepare_slot', 'human_review', 'execute_slot'],
            'create_private_slot': ['prepare_slot', 'human_review', 'execute_slot'],
            'advance_application': ['parser', 'matching', 'status', 'prepare_advance', 'human_review', 'execute_advance'],
            'screen_application': ['parser', 'matching', 'status', 'prepare_screen', 'human_review', 'execute_screen'],
            'advance_and_reserve_slot': ['parser', 'matching', 'status', 'prepare_advance', 'human_review', 'execute_advance'],
            'semantic_review': ['parser', 'matching', 'semantic', 'human_review'],
            'semantic_advance_and_reserve_slot': ['parser', 'matching', 'semantic', 'status', 'prepare_advance', 'human_review', 'execute_advance'],
        }
        return {'plan': plans[state.get('intent', 'full')]}

    def parser(state: ReviewState):
        with connect(path) as db:
            db.execute('BEGIN')
            source = masked_source(db, state['application_id'])
        blind_source = source['text']
        result = structure_resume(blind_source)
        return {"parsed": {"text_available": bool(blind_source.strip()),
                           "sections": {name: section["status"] for name, section in result["sections"].items()},
                           "notice": "Section mentions are unverified; redaction is incomplete. "
                                     "The structured résumé endpoint exposes text for HR review."},
                "parser_source_ref": source['source_sha256'], "parser_context_ref": source['context_sha256']}

    def matching(state: ReviewState):
        if authorize is None or user is None:
            raise HTTPException(status_code=403, detail='HR access required')
        application_id = state['application_id']
        authorize()
        with connect(path) as db:
            db.execute('BEGIN')
            try:
                job_id, job, source = _bounded_matching_inputs(db, application_id)
            except HTTPException as exc:
                if (state.get('intent', 'full') != 'full' or exc.status_code != 422
                        or not isinstance(exc.detail, dict) or exc.detail.get('code') != 'graph_matching_work_limit'):
                    raise
                application = get_application(db, application_id)
                job_id = application['job_id']
                source = masked_source(db, application_id)
                if state.get('parser_context_ref', state.get('parser_source_ref')) != source['context_sha256']:
                    raise HTTPException(status_code=409, detail='Resume changed; start a new review') from exc
                unavailable = {'job_id': job_id, 'source_context': source['context_sha256'],
                               'job_revision': _job_revision(db, job_id),
                               'assignment_revision': _assignment_revision(db, application_id),
                               'source_revisions': list(_source_revisions(db, application_id))}
                authorize()
                return {'matching_unavailable_ref': unavailable}
            parser_context = state.get('parser_context_ref', state.get('parser_source_ref'))
            if parser_context is not None and parser_context != source['context_sha256']:
                raise HTTPException(status_code=409, detail='Resume changed; start a new review')
            digest, _ = matching_snapshot(db, application_id)
            revisions = (_job_revision(db, job_id), _assignment_revision(db, application_id),
                         _source_revisions(db, application_id))
        if not _matching_workers.acquire(blocking=False):
            raise HTTPException(status_code=503, detail='Graph matching is busy; retry shortly')
        try:
            score = score_masked_resume(job, source)
        finally:
            _matching_workers.release()
        authorize()
        with connect(path) as db:
            db.execute('BEGIN')
            row = db.execute('SELECT job_id FROM applications WHERE id=?', (application_id,)).fetchone()
            current_digest, _ = matching_snapshot(db, application_id) if row else (None, None)
            current_revisions = (_job_revision(db, job_id), _assignment_revision(db, application_id),
                                 _source_revisions(db, application_id))
            if row is None or row['job_id'] != job_id or current_digest != digest or current_revisions != revisions:
                raise HTTPException(status_code=409, detail='Matching inputs changed; start a new review')
        authorize()
        # Evidence excerpts can include identifying data even after masking.
        for group in ("required", "preferred"):
            score[group] = [{"skill": item["skill"], "mentioned": item["mentioned"]}
                            for item in score[group]]
        return {"matching": score, "matching_specialist_ref": digest}

    def policy(state: ReviewState):
        question = policy_question.strip()
        if not question:
            return {"policy": {"status": "not_requested"}}
        if user is None or authorize is None:
            raise HTTPException(status_code=403, detail='HR access required')
        try:
            answer = run_policy_agent(path, user, question, enabled=enable_agent, authorize=authorize,
                                      allow_hybrid_answers=False).answer
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail='Policy access changed; please retry') from exc
        if answer.mode == 'abstain':
            return {"policy": {"status": "abstain"}}
        return {"policy": {"status": "source_refs_v2", "refs": [
            {"id": source.id, "digest": _digest(source)} for source in answer.sources
        ], "numeric_notice": bool(_numeric_notice(question, [source.text for source in answer.sources]))}}

    def semantic(state: ReviewState):
        if request is None or user is None or authorize is None:
            raise HTTPException(status_code=403, detail='HR access required')
        from app.rubric_admin import semantic_suggestions

        authorize()
        with connect(path) as db:
            original_context = semantic_context(db, state['application_id'])
        result = semantic_suggestions(state['application_id'], request, user)
        authorize()
        with connect(path) as db:
            matching_digest, _ = matching_snapshot(db, state['application_id'])
            if (semantic_context(db, state['application_id']) != original_context
                    or result['source_sha256'] != state.get('parser_source_ref')
                    or masked_source(db, state['application_id'])['context_sha256'] != state.get('parser_context_ref', state.get('parser_source_ref'))
                    or matching_digest != state.get('matching_specialist_ref')):
                raise HTTPException(status_code=409, detail='Application changed; start a new semantic review')
        return {'semantic_refs': save_references(result, original_context)}

    def scheduler(state: ReviewState):
        return {"scheduling": _current_scheduling(path, state["application_id"])}

    def status(state: ReviewState):
        with connect(path) as db:
            row = get_application(db, state["application_id"])
            result = {"status": {"application_status": row["status"]}}
            if state.get("intent", "full") == "full":
                result["provisional_queue_ref"] = state["application_id"]
            return result

    def agent_review(state: ReviewState):
        if not enable_agent or user is None:
            return {"agent_review": {"status": "disabled", "tool_trace": []}}
        result = run_agent(path, state["application_id"], user, 'prepare review',
                           enabled=True, authorize=authorize)
        # Never persist model output or tool payloads in the graph checkpoint.
        return {"agent_review": {"status": result['status'], "tool_trace": result['tool_trace']}}

    def prepare_slot(state: ReviewState):
        private = state.get('intent') == 'create_private_slot'
        with connect(path) as db:
            application = get_application(db, state['application_id'])
            if application['status'] != 'advanced':
                raise HTTPException(status_code=409, detail='Application is not eligible for candidate booking')
            proposal = {'type': 'create_private_slot' if private else 'create_slot', 'application_id': state['application_id'],
                        'operation_id': state['operation_id'], 'slot': state['slot'],
                        'candidate_id': application['candidate_id'],
                        'application_updated_at': application['updated_at'],
                        'availability_scope': 'selected_application_only' if private else 'all_eligible_candidates',
                        'notice': ('This reserves a slot for the selected application and candidate only. '
                                   'The candidate must book it separately; no invitation is sent.' if private else
                                  'This creates a shared slot available to all eligible candidates. '
                                  'It is not reserved for this application; the application is used '
                                  'to verify eligibility and conflicts.')}
        encoded = json.dumps(proposal, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        proposal['approval_fingerprint'] = hashlib.sha256(encoded).hexdigest()
        return {'action_proposal': proposal}

    def execute_slot(state: ReviewState):
        if not state['reviewed']:
            return {'action_result': {'status': 'declined'}}
        if action_db is None or user is None:
            raise RuntimeError('An authorized resume transaction is required')
        proposal = state['action_proposal']
        application = get_application(action_db, state['application_id'])
        if (application['status'] != 'advanced' or application['candidate_id'] != proposal['candidate_id']
                or application['updated_at'] != proposal['application_updated_at']):
            raise HTTPException(status_code=409, detail='Application changed; start a new review')
        slot = SlotCreate.model_validate(proposal['slot'])
        private = proposal.get('availability_scope') == 'selected_application_only'
        if private:
            item = BatchSlot.model_validate({**proposal['slot'], 'application_id': state['application_id']})
            candidates = _validate_batch(action_db, [item])
            if candidates[state['application_id']] != proposal['candidate_id']:
                raise HTTPException(status_code=409, detail='Candidate ownership changed; start a new review')
        start, end = utc_text(slot.start_at), utc_text(slot.end_at)
        if private_slot_conflict(action_db, start, end, slot.interviewer.casefold(),
                                 candidate_id=application['candidate_id']):
            raise HTTPException(status_code=409, detail='Candidate or interviewer has a reserved private slot')
        if action_db.execute("""SELECT 1 FROM interviews i WHERE i.status='scheduled' AND i.application_id=?
                AND NOT EXISTS (SELECT 1 FROM interview_completions c WHERE c.interview_id=i.id)""", (state['application_id'],)).fetchone():
            raise HTTPException(status_code=409, detail='Application already has an interview')
        if action_db.execute("""SELECT 1 FROM interviews WHERE status='scheduled' AND start_at < ? AND end_at > ?
                AND (candidate_id=? OR interviewer_key=?) AND NOT EXISTS
                (SELECT 1 FROM interview_completions c WHERE c.interview_id=interviews.id)""", (end, start, application['candidate_id'], slot.interviewer.casefold())).fetchone():
            raise HTTPException(status_code=409, detail='Candidate or interviewer has a conflicting interview')
        if action_db.execute("""SELECT 1 FROM interview_slots WHERE withdrawn=0 AND start_at < ? AND end_at > ?
                AND interviewer_key=?""", (end, start, slot.interviewer.casefold())).fetchone():
            raise HTTPException(status_code=409, detail='Interviewer has a conflicting slot')
        created = create_slot_record(action_db, slot, user.id)
        if private:
            action_db.execute('UPDATE interview_slots SET target_application_id=?, target_candidate_id=? WHERE id=?',
                              (state['application_id'], proposal['candidate_id'], created['id']))
            created['application_id'] = state['application_id']
        return {'action_result': {'status': 'created', 'slot': created}}

    def prepare_offer(state: ReviewState):
        if authorize is None or user is None:
            raise HTTPException(status_code=403, detail='HR access required')
        authorize()
        with connect(path) as db:
            db.execute('BEGIN')
            _, _, fingerprint = _offer_snapshot(db, state['application_id'], state['offer_draft_id'], user.id)
        authorize()
        return {'action_proposal': {'type': 'present_offer', 'application_id': state['application_id'],
                                    'approval_fingerprint': fingerprint,
                                    'notice': 'Present this offer locally only after HR approval; no message is sent.'}}

    def execute_offer(state: ReviewState):
        if action_db is None or user is None:
            raise RuntimeError('An authorized resume transaction is required')
        if not state['reviewed']:
            action_db.execute('DELETE FROM graph_offer_drafts WHERE thread_id=?', (state['offer_draft_id'],))
            return {'action_result': {'status': 'declined'}}
        details, _, fingerprint = _offer_snapshot(action_db, state['application_id'], state['offer_draft_id'], user.id)
        if not secrets.compare_digest(fingerprint, state['action_proposal']['approval_fingerprint']):
            raise HTTPException(status_code=409, detail='Offer or application changed; start a new review')
        offer = record_offer(action_db, state['application_id'], details, user.id)
        action_db.execute('DELETE FROM graph_offer_drafts WHERE thread_id=?', (state['offer_draft_id'],))
        return {'action_result': {'status': 'offer_presented', 'offer_id': offer['id']}}

    def prepare_rejection(state: ReviewState):
        if authorize is None or user is None:
            raise HTTPException(status_code=403, detail='HR access required')
        authorize()
        with connect(path) as db:
            db.execute('BEGIN')
            _, _, _, _, fingerprint = _rejection_snapshot(db, state['application_id'], state['rejection_draft_id'], user.id)
        authorize()
        return {'action_proposal': {'type': 'reject_application', 'application_id': state['application_id'],
                                    'approval_fingerprint': fingerprint,
                                    'notice': 'Review the reason and local effects before rejecting. No message is sent.'}}

    def execute_rejection(state: ReviewState):
        if action_db is None or user is None:
            raise RuntimeError('An authorized resume transaction is required')
        if not state['reviewed']:
            action_db.execute('DELETE FROM graph_rejection_drafts WHERE thread_id=?', (state['rejection_draft_id'],))
            return {'action_result': {'status': 'declined'}}
        reason, _, effects, _, fingerprint = _rejection_snapshot(action_db, state['application_id'], state['rejection_draft_id'], user.id)
        if not secrets.compare_digest(fingerprint, state['action_proposal']['approval_fingerprint']):
            raise HTTPException(status_code=409, detail='Rejection context changed; start a new review')
        record_rejection(action_db, state['application_id'], user.id, reason)
        _check_rejection_effects(action_db, effects)
        action_db.execute('DELETE FROM graph_rejection_drafts WHERE thread_id=?', (state['rejection_draft_id'],))
        return {'action_result': {'status': 'rejected'}}

    def prepare_hire(state: ReviewState):
        if authorize is None or user is None:
            raise HTTPException(status_code=403, detail='HR access required')
        authorize()
        with connect(path) as db:
            db.execute('BEGIN')
            offer, _, fingerprint = _hire_snapshot(db, state['application_id'])
        authorize()
        return {'action_proposal': {'type': 'finalize_hire', 'application_id': state['application_id'],
                                    'offer_id': offer['id'], 'approval_fingerprint': fingerprint,
                                    'notice': 'Confirm hiring after reviewing the accepted offer; no message is sent.'}}

    def execute_hire(state: ReviewState):
        if action_db is None or user is None:
            raise RuntimeError('An authorized resume transaction is required')
        if not state['reviewed']:
            return {'action_result': {'status': 'declined'}}
        _, _, fingerprint = _hire_snapshot(action_db, state['application_id'])
        if not secrets.compare_digest(fingerprint, state['action_proposal']['approval_fingerprint']):
            raise HTTPException(status_code=409, detail='Offer or application changed; start a new review')
        record_hire(action_db, state['application_id'], user.id)
        return {'action_result': {'status': 'hired'}}

    def prepare_completion(state: ReviewState):
        if authorize is None or user is None:
            raise HTTPException(status_code=403, detail='HR access required')
        authorize()
        with connect(path) as db:
            db.execute('BEGIN')
            row, fingerprint = _interview_completion_snapshot(db, state['application_id'], state['interview_id'])
            proposal = {'type': 'complete_interview', 'application_id': state['application_id'],
                        'interview_id': row['id'], 'approval_fingerprint': fingerprint,
                        'notice': 'Confirm only if the interview took place. No message is sent.'}
        authorize()
        return {'action_proposal': proposal}

    def execute_completion(state: ReviewState):
        if not state['reviewed']:
            return {'action_result': {'status': 'declined'}}
        if action_db is None or user is None:
            raise RuntimeError('An authorized resume transaction is required')
        _, current = _interview_completion_snapshot(action_db, state['application_id'], state['interview_id'])
        if not secrets.compare_digest(current, state['action_proposal']['approval_fingerprint']):
            raise HTTPException(status_code=409, detail='Interview or application changed; start a new review')
        record_interview_completion(action_db, state['interview_id'], user.id)
        return {'action_result': {'status': 'interview_completed', 'interview_id': state['interview_id']}}

    def prepare_screen(state: ReviewState):
        with connect(path) as db:
            db.execute('BEGIN')
            application, score, context_digest, revision, score_digest = _screen_snapshot(db, state['application_id'])
            parser_source = masked_source(db, state['application_id'])
            matching_digest, _ = matching_snapshot(db, state['application_id'])
            projected = dict(score)
            for group in ('required', 'preferred'):
                projected[group] = [{'skill': item['skill'], 'mentioned': item['mentioned']} for item in score[group]]
            if (application['status'] != 'submitted' or application['score_json'] or
                    state.get('status', {}).get('application_status') != 'submitted' or
                    state.get('parser_source_ref') != parser_source['source_sha256'] or
                    state.get('parser_context_ref', state.get('parser_source_ref')) != parser_source['context_sha256'] or
                    state.get('matching_specialist_ref') != matching_digest or state.get('matching') != projected):
                raise HTTPException(status_code=409, detail='A current, unscreened submitted application is required')
            proposal = {'type': 'screen_application', 'application_id': state['application_id'],
                        'candidate_id': application['candidate_id'], 'job_id': application['job_id'],
                        'job_title': db.execute('SELECT title FROM jobs WHERE id=?', (application['job_id'],)).fetchone()['title'],
                        'current_status': 'submitted',
                        'notice': 'Approval records the reviewed keyword screening. No hiring decision or message is made.'}
            encoded = json.dumps({'proposal': proposal, 'context': context_digest, 'revision': revision,
                                  'score': score_digest}, ensure_ascii=False, sort_keys=True,
                                 separators=(',', ':')).encode('utf-8')
            proposal['approval_fingerprint'] = hashlib.sha256(encoded).hexdigest()
            return {'action_proposal': proposal, 'context_digest': context_digest,
                    'screen_revision': revision, 'screen_score_digest': score_digest}

    def execute_screen(state: ReviewState):
        if not state['reviewed']:
            return {'action_result': {'status': 'declined'}}
        if action_db is None or user is None:
            raise RuntimeError('An authorized resume transaction is required')
        application, score, context_digest, revision, score_digest = _screen_snapshot(action_db, state['application_id'])
        if not _screen_is_current(state, application, context_digest, revision, score_digest):
            raise HTTPException(status_code=409, detail='Application or screening changed; start a new review')
        store_screening(action_db, state['application_id'], user.id, score)
        return {'action_result': {'status': 'screened'}}

    def prepare_advance(state: ReviewState):
        with connect(path) as db:
            db.execute('BEGIN')
            application = get_application(db, state['application_id'])
            _, context_digest = context_for(db, state['application_id'])
            matching_digest, assessment = matching_snapshot(db, state['application_id'])
            revision_row = db.execute('SELECT revision FROM rubric_publication_revisions WHERE application_id=?',
                                      (state['application_id'],)).fetchone()
            revision = revision_row['revision'] if revision_row else 0
            parser_source = masked_source(db, state['application_id'])
            if (application['status'] != 'submitted' or assessment['status'] != 'current' or
                    state.get('matching_specialist_ref') != matching_digest or
                    state.get('parser_source_ref') != parser_source['source_sha256'] or
                    state.get('parser_context_ref', state.get('parser_source_ref')) != parser_source['context_sha256'] or
                    state.get('status', {}).get('application_status') != 'submitted'):
                raise HTTPException(status_code=409, detail='A current reviewed assessment and submitted application are required')
            proposal = {'type': 'advance_application', 'application_id': state['application_id'],
                        'current_status': 'submitted', 'target_status': 'advanced',
                        'assessment_id': assessment['assessment_id'],
                        'notice': 'Approval advances this application for interview scheduling. '
                                  'The candidate must choose a slot separately; no message is sent.'}
            if state.get('intent') in ('advance_and_reserve_slot', 'semantic_advance_and_reserve_slot') or state.get('adaptive_goal') == 'advance_and_reserve':
                if ((state.get('intent') == 'semantic_advance_and_reserve_slot' or
                     (state.get('adaptive_goal') == 'advance_and_reserve' and 'semantic' in state.get('plan', []))) and
                        restore_references(db, state['application_id'], state.get('semantic_refs'))['status'] != 'current'):
                    raise HTTPException(status_code=409, detail='Resume or criteria changed; start a new review')
                proposal.update({'type': 'advance_and_reserve_slot', 'candidate_id': application['candidate_id'],
                                 'availability_scope': 'selected_application_only', 'slot': state['slot'],
                                 'notice': 'Approval advances this application and reserves this slot for its candidate. '
                                           'The candidate must book separately; no message is sent.'})
            encoded = json.dumps({'proposal': proposal, 'context': context_digest, 'assessment': matching_digest,
                                  'assessment_revision': revision, 'semantic_refs': state.get('semantic_refs')},
                                 ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
            proposal['approval_fingerprint'] = hashlib.sha256(encoded).hexdigest()
            return {'action_proposal': proposal, 'context_digest': context_digest, 'assessment_revision': revision}

    def execute_advance(state: ReviewState):
        if not state['reviewed']:
            return {'action_result': {'status': 'declined'}}
        if action_db is None or user is None:
            raise RuntimeError('An authorized resume transaction is required')
        application = get_application(action_db, state['application_id'])
        _, context_digest = context_for(action_db, state['application_id'])
        matching_digest, assessment = matching_snapshot(action_db, state['application_id'])
        revision_row = action_db.execute('SELECT revision FROM rubric_publication_revisions WHERE application_id=?',
                                         (state['application_id'],)).fetchone()
        revision = revision_row['revision'] if revision_row else 0
        if (application['status'] != 'submitted' or context_digest != state.get('context_digest') or
                matching_digest != state.get('matching_specialist_ref') or assessment['status'] != 'current' or
                revision != state.get('assessment_revision') or
                ((state.get('intent') in ('advance_and_reserve_slot', 'semantic_advance_and_reserve_slot') or state.get('adaptive_goal') == 'advance_and_reserve') and
                 application['candidate_id'] != state['action_proposal']['candidate_id']) or
                assessment['assessment_id'] != state['action_proposal']['assessment_id']):
            raise HTTPException(status_code=409, detail='Application or assessment changed; start a new review')
        action_db.execute("UPDATE applications SET status='advanced', updated_at=strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id=?",
                          (state['application_id'],))
        details = json.dumps({'decision': 'advance', 'note': ''}, ensure_ascii=False)
        action_db.execute('INSERT INTO audit_events(application_id,actor_id,action,details) VALUES (?,?,?,?)',
                          (state['application_id'], user.id, 'decision', details))
        if state.get('intent') in ('advance_and_reserve_slot', 'semantic_advance_and_reserve_slot') or state.get('adaptive_goal') == 'advance_and_reserve':
            current = get_application(action_db, state['application_id'])
            slot_state = {**state, 'action_proposal': {**state['action_proposal'],
                'application_updated_at': current['updated_at']}}
            slot_result = execute_slot(slot_state)['action_result']
            return {'action_result': {'status': 'advanced_and_reserved', 'slot': slot_result['slot']}}
        return {'action_result': {'status': 'advanced'}}

    def human_review(state: ReviewState):
        notice = ('Confirm rejection after reviewing its reason and local effects. No message is sent.'
                  if state.get('intent') == 'reject_application' else
                  'Confirm hiring after reviewing the accepted offer. No message is sent.'
                  if state.get('intent') == 'finalize_hire' else
                  'Present this offer locally. No message is sent.'
                  if state.get('intent') == 'present_offer' else
                  'Confirm this interview took place. No message is sent.'
                  if state.get('intent') == 'complete_interview' else
                  'Approve recording this keyword screening. No hiring decision or message is made.'
                  if state.get('intent') == 'screen_application' else
                  'Approve advancement and a private slot for this candidate. No message is sent.'
                  if state.get('intent') in ('advance_and_reserve_slot', 'semantic_advance_and_reserve_slot') or state.get('adaptive_goal') == 'advance_and_reserve' else
                  'Approve advancing this application for interview scheduling. No message is sent.'
                  if state.get('intent') == 'advance_application' else
                  'Approve creation of a slot for this application and candidate only. '
                  'The candidate must book it separately; no invitation is sent.'
                  if state.get('intent') == 'create_private_slot' else
                  'Approve creation of a shared slot available to all eligible candidates. '
                  'It is not reserved for this application; a candidate must book it separately.'
                  if state.get('intent') == 'create_slot' or state.get('adaptive_goal') == 'schedule_interview' else
                  'Acknowledge review; no hiring or booking action will be taken.')
        reviewed = interrupt({"type": "human_review", "application_id": state["application_id"],
                              "notice": notice, "action_proposal": state.get('action_proposal')})
        return {"reviewed": reviewed}

    builder = StateGraph(ReviewState)
    nodes = {"plan": plan, "parser": parser, "matching": matching, "semantic": semantic, "policy": policy,
             "scheduler": scheduler, "status": status, "agent_review": agent_review,
             "human_review": human_review, "prepare_slot": prepare_slot, "execute_slot": execute_slot,
             "prepare_advance": prepare_advance, "execute_advance": execute_advance,
             "prepare_screen": prepare_screen, "execute_screen": execute_screen,
             "prepare_completion": prepare_completion, "execute_completion": execute_completion,
             "prepare_offer": prepare_offer, "execute_offer": execute_offer,
             "prepare_hire": prepare_hire, "execute_hire": execute_hire,
             "prepare_rejection": prepare_rejection, "execute_rejection": execute_rejection}
    def guarded(node):
        def execute(state):
            if state.get('intent') != 'adaptive':
                return node(state)
            if authorize is None:
                raise HTTPException(status_code=403, detail='HR access required')
            authorize()
            with connect(path) as db:
                if context_for(db, state['application_id'])[1] != state['context_digest']:
                    raise HTTPException(status_code=409, detail='Application changed; start a new review')
            result = node(state)
            authorize()
            with connect(path) as db:
                if context_for(db, state['application_id'])[1] != state['context_digest']:
                    raise HTTPException(status_code=409, detail='Application changed; start a new review')
            return result
        return execute

    for name, node in nodes.items():
        builder.add_node(name, guarded(node) if name in {'parser', 'matching', 'semantic', 'policy', 'status', 'scheduler', 'prepare_slot'} else node)
    builder.add_edge(START, 'plan')
    def next_after(name):
        def route(state: ReviewState):
            steps = state['plan']
            return steps[0] if name == 'plan' else steps[steps.index(name) + 1]
        return route
    choices = ['parser', 'matching', 'semantic', 'policy', 'scheduler', 'status', 'agent_review', 'prepare_slot', 'prepare_advance', 'prepare_screen', 'prepare_completion', 'prepare_offer', 'human_review', 'execute_slot', 'execute_advance', 'execute_screen', 'execute_completion', 'execute_offer', 'prepare_hire', 'execute_hire', 'prepare_rejection', 'execute_rejection']
    for name in ['plan', 'parser', 'matching', 'semantic', 'policy', 'scheduler', 'status', 'agent_review', 'prepare_slot', 'prepare_advance', 'prepare_screen', 'prepare_completion', 'prepare_offer', 'prepare_hire', 'prepare_rejection']:
        builder.add_conditional_edges(name, next_after(name), choices)
    builder.add_conditional_edges('human_review', lambda state: 'execute_rejection' if state.get('intent') == 'reject_application' else 'execute_hire' if state.get('intent') == 'finalize_hire' else 'execute_offer' if state.get('intent') == 'present_offer' else 'execute_completion' if state.get('intent') == 'complete_interview' else 'execute_screen' if state.get('intent') == 'screen_application' else 'execute_advance' if state.get('intent') in ('advance_application', 'advance_and_reserve_slot', 'semantic_advance_and_reserve_slot') or state.get('adaptive_goal') == 'advance_and_reserve' else 'execute_slot' if state.get('intent') in ('create_slot', 'create_private_slot') or state.get('adaptive_goal') == 'schedule_interview' else END,
                                  {'execute_slot': 'execute_slot', 'execute_advance': 'execute_advance', 'execute_screen': 'execute_screen', 'execute_completion': 'execute_completion', 'execute_offer': 'execute_offer', 'execute_hire': 'execute_hire', 'execute_rejection': 'execute_rejection', END: END})
    builder.add_edge('execute_slot', END)
    builder.add_edge('execute_advance', END)
    builder.add_edge('execute_screen', END)
    builder.add_edge('execute_completion', END)
    builder.add_edge('execute_offer', END)
    builder.add_edge('execute_hire', END)
    builder.add_edge('execute_rejection', END)
    return builder.compile(checkpointer=checkpointer)


def _owner(path: str | Path, thread_id: str, user: User) -> None:
    # Look up ownership independently of model-generated state and hide other IDs.
    with connect(path) as db:
        row = db.execute("SELECT 1 FROM graph_review_owners WHERE thread_id = ? AND owner_id = ?",
                         (thread_id, user.id)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Review not found")


def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def _adaptive_policy_current(db, policy: object) -> bool:
    """An acknowledgment must still refer to authorized, unchanged sources."""
    if not isinstance(policy, dict):
        return False
    if policy.get('status') == 'abstain':
        return True
    if policy.get('status') != 'source_refs_v2':
        return False
    refs = policy.get('refs')
    if not isinstance(refs, list) or not 1 <= len(refs) <= 3:
        return False
    available = {source.id: source for source in authorized_policy_sources(db, 'hr')}
    return all(isinstance(ref, dict) and set(ref) == {'id', 'digest'}
               and isinstance(ref['id'], str) and ref['id'] in available
               and ref['digest'] == _digest(available[ref['id']]) for ref in refs)


@contextmanager
def _review_lock(path: str | Path, thread_id: str):
    """Acquire a local OS lock, released even if the owning process crashes.

    Lock files are retained: deleting them can split concurrent processes
    across different inodes. Shared/network filesystems require lock support.
    """
    database = Path(path).resolve()
    directory = database.parent / ".graph-review-locks"
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(f"{database}\0{thread_id}".encode()).hexdigest()
    with (directory / digest).open("a+b") as file:
        file.seek(0, os.SEEK_END)
        if file.tell() == 0:
            file.write(b"\0")
            file.flush()
        file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise HTTPException(status_code=409, detail="Review is already being resumed") from exc
            raise
        try:
            yield
        finally:
            if os.name == "nt":
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)


def _view_revisions(db, application):
    """Bind returned view data to mutable inputs without retaining source text."""
    application_id = application['id']
    rubric = db.execute('SELECT revision FROM rubric_publication_revisions WHERE application_id=?',
                        (application_id,)).fetchone()
    # Legacy views can be restored before graph-specific tables are initialized.
    has_screen_revisions = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='graph_screen_revisions'"
    ).fetchone()
    screening = (db.execute('SELECT revision FROM graph_screen_revisions WHERE application_id=?',
                            (application_id,)).fetchone() if has_screen_revisions else None)
    return (application['candidate_id'], application['status'], application['job_id'],
            _source_revisions(db, application_id), _assignment_revision(db, application_id),
            _job_revision(db, application['job_id']), rubric['revision'] if rubric else 0,
            screening['revision'] if screening else 0)


def _view(snapshot, path: str | Path | None = None, *, request: Request | None = None,
          user: User | None = None) -> dict:
    state = dict(snapshot.values)
    specialist_ref = state.pop('matching_specialist_ref', None)
    unavailable_ref = state.pop('matching_unavailable_ref', None)
    provisional_ref = state.pop('provisional_queue_ref', None)
    # Lifecycle is always reconstructed from current authorized data, never checkpoint content.
    state.pop('candidate_progress', None)
    candidate_progress_owner = None
    candidate_progress_status = None
    view_revisions = None
    displayed_policy_refs = None
    parser_ref = state.pop('parser_source_ref', None)
    parser_context_ref = state.pop('parser_context_ref', parser_ref)
    semantic_refs = state.pop('semantic_refs', None)
    # Old checkpoints lack intent and retain the historical full-review view.
    # Never expose stale stage data outside an explicitly selected scope.
    selected = state.get('intent', 'full')
    scoped_fields = {'parsed': 'full', 'matching': 'full', 'agent_review': 'full',
                     'policy': 'policy', 'scheduling': 'scheduling', 'status': 'status'}
    if selected not in {'full', 'policy', 'scheduling', 'status', 'create_slot', 'create_private_slot', 'advance_application', 'advance_and_reserve_slot', 'semantic_review', 'semantic_advance_and_reserve_slot', 'adaptive', 'screen_application', 'complete_interview', 'present_offer', 'finalize_hire', 'reject_application'}:
        selected = 'invalid'
    for field, scope in scoped_fields.items():
        adaptive_fields = {'parsed': 'parser', 'matching': 'matching', 'policy': 'policy', 'scheduling': 'scheduler', 'status': 'status'}
        if selected == 'adaptive' and adaptive_fields.get(field) in state.get('plan', []):
            continue
        if selected in ('advance_application', 'advance_and_reserve_slot', 'semantic_review', 'semantic_advance_and_reserve_slot', 'screen_application') and field in {'parsed', 'matching', 'status'}:
            continue
        if selected != 'full' and scope != selected:
            state.pop(field, None)
    if 'matching' in state and not isinstance(specialist_ref, str):
        state.pop('matching', None)
        state['matching_specialist'] = {
            'status': 'stale', 'notice': 'This older matching review cannot be verified; start a new review.'}
    # Old checkpoints may contain data from a previous version of the graph.
    # Never return their question, answer, or source metadata to a caller.
    state.pop("policy_question", None)
    if "matching" in state:
        matching = dict(state["matching"])
        for group in ("required", "preferred"):
            if group in matching:
                matching[group] = [{"skill": item["skill"], "mentioned": item["mentioned"]}
                                   for item in matching[group]]
        state["matching"] = matching
    policy = {}
    if "policy" in state:
        policy = state["policy"] if isinstance(state["policy"], dict) else {}
        status = policy.get("status")
        state["policy"] = {"status": status if status in {
            "not_requested", "searched_without_retaining_results", "abstain"
        } else "unavailable"}
        if status == 'abstain':
            state['policy']['next_step'] = POLICY_CONTACT_HR
    if path is not None and "application_id" in state:
        with connect(path) as db:
            db.execute('BEGIN')
            if request is not None and user is not None:
                token = request.cookies.get(COOKIE_NAME, '')
                actor = db.execute('''SELECT u.id, u.username, u.role, u.active FROM sessions s
                    JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.user_id=? AND s.expires_at>?''',
                    (token_digest(token), user.id, int(time.time()))).fetchone()
                if actor is None or not actor['active'] or actor['username'] != user.username:
                    raise HTTPException(status_code=401, detail='Authentication required')
                if actor['role'] != 'hr':
                    raise HTTPException(status_code=403, detail='HR access required')
                if (selected == 'full' and type(provisional_ref) is int
                        and provisional_ref == state['application_id']):
                    state['provisional_screening'] = provisional_application_state(db, provisional_ref)
                    state['provisional_screening']['notice'] = (
                        'Live queue information, not part of the reviewed graph snapshot. '
                        'A provisional keyword score is not verified ability or an HR decision. '
                        'The current threshold and source review status determine prioritization.')
                if selected == 'present_offer' and snapshot.next:
                    try:
                        details, identity, fingerprint = _offer_snapshot(db, state['application_id'], state['offer_draft_id'], actor['id'])
                        if not secrets.compare_digest(fingerprint, state['action_proposal']['approval_fingerprint']):
                            raise HTTPException(status_code=409, detail='Offer changed')
                        state['offer_preview'] = {'status': 'current', 'details': details, **identity,
                                                  'notice': 'Review exact offer details before approval.'}
                    except (HTTPException, KeyError):
                        state['offer_preview'] = {'status': 'stale', 'notice': 'Offer or application changed; start a new review.'}
                if selected == 'reject_application' and snapshot.next:
                    try:
                        reason, identity, effects, current_status, fingerprint = _rejection_snapshot(
                            db, state['application_id'], state['rejection_draft_id'], actor['id'])
                        if not secrets.compare_digest(fingerprint, state['action_proposal']['approval_fingerprint']):
                            raise HTTPException(status_code=409, detail='Rejection changed')
                        state['rejection_preview'] = {'status': 'current', 'reason': reason, **identity,
                                                      'current_status': current_status, **effects}
                    except (HTTPException, KeyError):
                        state['rejection_preview'] = {'status': 'stale', 'notice': 'Rejection context changed; start a new review.'}
                if selected == 'finalize_hire' and snapshot.next:
                    try:
                        offer, identity, fingerprint = _hire_snapshot(db, state['application_id'])
                        if not secrets.compare_digest(fingerprint, state['action_proposal']['approval_fingerprint']):
                            raise HTTPException(status_code=409, detail='Offer changed')
                        state['hire_preview'] = {'status': 'current', 'offer_id': offer['id'],
                                                 'details': offer['details'], **identity}
                    except (HTTPException, KeyError):
                        state['hire_preview'] = {'status': 'stale', 'notice': 'Offer or application changed; start a new review.'}
                if selected == 'complete_interview' and snapshot.next:
                    try:
                        interview, fingerprint = _interview_completion_snapshot(db, state['application_id'], state['interview_id'])
                        if not secrets.compare_digest(fingerprint, state['action_proposal']['approval_fingerprint']):
                            raise HTTPException(status_code=409, detail='Interview changed')
                        state['interview_preview'] = {'status': 'current', 'id': interview['id'],
                            'start_at': interview['start_at'], 'end_at': interview['end_at'],
                            'interviewer': interview['interviewer'],
                            'notice': 'Confirm only if this interview took place.'}
                    except (HTTPException, KeyError):
                        state['interview_preview'] = {'status': 'stale', 'notice': 'Interview changed; start a new review.'}
                if selected == 'screen_application' and snapshot.next:
                    application, score, context_digest, revision, score_digest = _screen_snapshot(db, state['application_id'])
                    if _screen_is_current(state, application, context_digest, revision, score_digest):
                        state['screening_preview'] = {'status': 'current', 'score': score}
                    else:
                        state['screening_preview'] = {'status': 'stale', 'notice': 'Application or screening changed; start a new review.'}
                        state.pop('matching', None)
                if selected in ('semantic_review', 'semantic_advance_and_reserve_slot') or (selected == 'adaptive' and 'semantic' in state.get('plan', [])):
                    state['semantic_suggestions'] = restore_references(db, state['application_id'], semantic_refs)
                if 'parsed' in state:
                    source = masked_source(db, state['application_id'])
                    if (isinstance(parser_ref, str) and isinstance(parser_context_ref, str)
                            and secrets.compare_digest(source['source_sha256'], parser_ref)
                            and secrets.compare_digest(source['context_sha256'], parser_context_ref)):
                        state['resume_fields'] = resume_fields_for_display(source['text'])
                    else:
                        state.pop('parsed', None)
                        state['resume_fields'] = {'status': 'stale', 'notice': 'Resume changed or cannot be verified; start a new review.'}
                if (selected in ('full', 'adaptive', 'advance_application', 'advance_and_reserve_slot', 'semantic_review', 'semantic_advance_and_reserve_slot', 'screen_application') and 'matching' in state.get('plan', [])
                        and isinstance(specialist_ref, str)):
                    digest, report = matching_snapshot(db, state['application_id'])
                    state['matching_specialist'] = report if secrets.compare_digest(
                        digest.encode('utf-8'), specialist_ref.encode('utf-8')) else {
                            'status': 'stale', 'notice': 'The matching inputs changed; start a new review.'}
                    state['interview_guide'] = guide_from_matching(state['matching_specialist'])
                    if state['matching_specialist']['status'] == 'stale':
                        state.pop('matching', None)
                if selected == 'full' and isinstance(unavailable_ref, dict):
                    application = get_application(db, state['application_id'])
                    current = masked_source(db, state['application_id'])
                    current_ref = {'job_id': application['job_id'], 'source_context': current['context_sha256'],
                                   'job_revision': _job_revision(db, application['job_id']),
                                   'assignment_revision': _assignment_revision(db, state['application_id']),
                                   'source_revisions': list(_source_revisions(db, state['application_id']))}
                    if unavailable_ref == current_ref:
                        state['matching_unavailable'] = {'status': 'limit_exceeded',
                            'notice': 'Keyword matching was not run because this résumé or job exceeds the work limit. Review manually; no score was produced.'}
                    else:
                        state['matching_unavailable'] = {'status': 'stale',
                            'notice': 'Matching inputs changed; start a new review.'}
                if policy.get('status') in {'source_refs_v1', 'source_refs_v2'}:
                    refs = policy.get('refs')
                    if policy['status'] == 'source_refs_v2':
                        available = {source.id: source for source in authorized_policy_sources(db, 'hr')}
                    else:
                        available = _authorized_sources(db, 'hr')
                    if (isinstance(refs, list) and 1 <= len(refs) <= 3 and
                            all(isinstance(ref, dict) and set(ref) == {'id', 'digest'} and
                                isinstance(ref['id'], str) and ref['id'] in available and
                                ref['digest'] == _digest(available[ref['id']]) for ref in refs)):
                        sources = [available[ref['id']] for ref in refs]
                        displayed_policy_refs = (policy['status'], refs)
                        state['policy'] = {'status': 'answer',
                            'answer': '\n\n'.join(f'[{s.id}] {s.text}' for s in sources),
                            'sources': [asdict(source) for source in sources],
                            'notice': ' '.join(filter(None, ('Extractive excerpts; review the source for relevance.',
                                NUMERIC_NOTICE if policy.get('numeric_notice') is True else '')))}
            application = get_application(db, state["application_id"])
            if request is not None and user is not None:
                view_revisions = _view_revisions(db, application)
            if selected == 'adaptive':
                state['plan_stale'] = context_for(db, state['application_id'])[1] != state.get('context_digest')
            state["status"] = {"application_status": application["status"]}
            if (request is not None and user is not None and 'status' in state.get('plan', [])
                    and selected in ('full', 'status', 'adaptive', 'advance_application',
                                     'advance_and_reserve_slot', 'semantic_advance_and_reserve_slot',
                                     'screen_application')):
                progress = candidate_stage(db, application['id'], application['candidate_id'])
                candidate_progress_owner = application['candidate_id']
                candidate_progress_status = application['status']
                state['candidate_progress'] = {
                    'stage': progress['stage'],
                    'notice': 'Live candidate-visible stage, reconstructed from current records. '
                              'An interview is completed only when HR records completion.',
                }
        if selected in ('full', 'scheduling', 'adaptive') and 'scheduler' in state.get('plan', []):
            state["scheduling"] = _current_scheduling(path, state["application_id"])
        else:
            state.pop('scheduling', None)
        if selected not in ('full', 'status', 'adaptive', 'advance_application', 'advance_and_reserve_slot', 'semantic_advance_and_reserve_slot', 'screen_application') or 'status' not in state.get('plan', []):
            state.pop('status', None)
    if selected in ('adaptive', 'advance_application', 'advance_and_reserve_slot', 'semantic_advance_and_reserve_slot', 'screen_application'):
        state.pop('context_digest', None)
        state.pop('assessment_revision', None)
        state.pop('screen_revision', None)
        state.pop('screen_score_digest', None)
    if path is not None and request is not None and user is not None:
        # A new read transaction sees session revocation committed after the
        # earlier graph-view snapshot, including when SQLite uses WAL mode.
        with connect(path) as fresh:
            fresh.execute('BEGIN')
            _require_current_role(fresh, request, user, 'hr')
            if view_revisions is not None:
                current_application = get_application(fresh, state['application_id'])
                if _view_revisions(fresh, current_application) != view_revisions:
                    raise HTTPException(status_code=409, detail='Review inputs changed; reload the review')
            if displayed_policy_refs is not None:
                ref_version, refs = displayed_policy_refs
                if ref_version == 'source_refs_v2':
                    available = {source.id: source for source in authorized_policy_sources(fresh, 'hr')}
                else:
                    available = _authorized_sources(fresh, 'hr')
                if any(ref['id'] not in available or _digest(available[ref['id']]) != ref['digest'] for ref in refs):
                    raise HTTPException(status_code=409, detail='Policy sources changed; reload the review')
            if selected == 'present_offer' and snapshot.next and state.get('offer_preview', {}).get('status') == 'current':
                try:
                    _, _, fingerprint = _offer_snapshot(fresh, state['application_id'], state['offer_draft_id'], user.id)
                    if not secrets.compare_digest(fingerprint, state['action_proposal']['approval_fingerprint']):
                        raise HTTPException(status_code=409, detail='Offer changed')
                except (HTTPException, KeyError):
                    raise HTTPException(status_code=409, detail='Offer changed; reload the review') from None
            if selected == 'reject_application' and snapshot.next and state.get('rejection_preview', {}).get('status') == 'current':
                try:
                    _, _, _, _, fingerprint = _rejection_snapshot(fresh, state['application_id'], state['rejection_draft_id'], user.id)
                    if not secrets.compare_digest(fingerprint, state['action_proposal']['approval_fingerprint']):
                        raise HTTPException(status_code=409, detail='Rejection changed')
                except (HTTPException, KeyError):
                    raise HTTPException(status_code=409, detail='Rejection changed; reload the review') from None
            if selected == 'finalize_hire' and snapshot.next and state.get('hire_preview', {}).get('status') == 'current':
                try:
                    _, _, fingerprint = _hire_snapshot(fresh, state['application_id'])
                    if not secrets.compare_digest(fingerprint, state['action_proposal']['approval_fingerprint']):
                        raise HTTPException(status_code=409, detail='Offer changed')
                except (HTTPException, KeyError):
                    raise HTTPException(status_code=409, detail='Offer changed; reload the review') from None
            if selected == 'complete_interview' and snapshot.next and state.get('interview_preview', {}).get('status') == 'current':
                try:
                    _, fingerprint = _interview_completion_snapshot(fresh, state['application_id'], state['interview_id'])
                    if not secrets.compare_digest(fingerprint, state['action_proposal']['approval_fingerprint']):
                        raise HTTPException(status_code=409, detail='Interview changed')
                except (HTTPException, KeyError):
                    raise HTTPException(status_code=409, detail='Interview changed; reload the review') from None
            if candidate_progress_owner is not None and 'candidate_progress' in state:
                current = fresh.execute('SELECT candidate_id,status FROM applications WHERE id=?',
                                        (state['application_id'],)).fetchone()
                if (current is None or current['candidate_id'] != candidate_progress_owner
                        or current['status'] != candidate_progress_status):
                    raise HTTPException(status_code=409, detail='Application changed; reload the review')
                progress = candidate_stage(fresh, state['application_id'], current['candidate_id'])
                if progress['stage'] != state['candidate_progress']['stage']:
                    raise HTTPException(status_code=409, detail='Application stage changed; reload the review')
    return {"state": state, "awaiting_review": bool(snapshot.next)}


@router.post("", status_code=201)
def start_review(payload: ReviewRequest, request: Request, user: HR):
    path = request.app.state.database_path
    initialize_graph_reviews(path)
    thread_id = secrets.token_urlsafe(24)
    with connect(path) as db:
        get_application(db, payload.application_id)
        _require_current_role(db, request, user, 'hr')
        db.execute("INSERT INTO graph_review_owners (thread_id, application_id, owner_id) VALUES (?, ?, ?)",
                   (thread_id, payload.application_id, user.id))
        if payload.intent == 'present_offer':
            db.execute('DELETE FROM graph_offer_drafts WHERE expires_at<=?', (int(time.time()),))
            db.execute('INSERT INTO graph_offer_drafts(thread_id,application_id,owner_id,details,expires_at) VALUES(?,?,?,?,?)',
                       (thread_id, payload.application_id, user.id, payload.offer_details.strip(), int(time.time()) + 86400))
        if payload.intent == 'reject_application':
            db.execute('DELETE FROM graph_rejection_drafts WHERE expires_at<=?', (int(time.time()),))
            db.execute('INSERT INTO graph_rejection_drafts(thread_id,application_id,owner_id,reason,expires_at) VALUES(?,?,?,?,?)',
                       (thread_id, payload.application_id, user.id, payload.rejection_reason.strip(), int(time.time()) + 86400))
        _require_current_role(db, request, user, 'hr')
    try:
        with SqliteSaver.from_conn_string(str(path)) as saver:
            def authorize():
                current = get_current_user(request)
                if current.id != user.id or current.role != 'hr':
                    raise HTTPException(status_code=403, detail='HR access required')

            graph = build_review_graph(path, saver, policy_question=payload.policy_question,
                                       user=user, authorize=authorize,
                                       enable_agent=os.environ.get('TAWZEEF_ENABLE_GROQ') == '1', request=request)
            config = _config(thread_id)
            initial = {"application_id": payload.application_id, "intent": payload.intent}
            if payload.intent == 'present_offer':
                initial['offer_draft_id'] = thread_id
            if payload.intent == 'reject_application':
                initial['rejection_draft_id'] = thread_id
            if payload.interview_id is not None:
                initial['interview_id'] = payload.interview_id
            if payload.allow_semantic:
                initial['allow_semantic'] = True
            if payload.adaptive_goal is not None:
                initial['adaptive_goal'] = payload.adaptive_goal
            if payload.slot is not None:
                initial.update(slot=payload.slot.model_dump(mode='json'), operation_id=thread_id)
            graph.invoke(initial, config)
            authorize()
            return {"id": thread_id, **_view(graph.get_state(config), path, request=request, user=user)}
    except Exception as exc:
        # A start that never returned its ID must not leave inaccessible data.
        try:
            with SqliteSaver.from_conn_string(str(path)) as saver:
                saver.delete_thread(thread_id)
        except Exception:
            logger.exception("Could not remove checkpoints after review start failed")
        try:
            with connect(path) as db:
                db.execute("DELETE FROM graph_offer_drafts WHERE thread_id = ?", (thread_id,))
                db.execute("DELETE FROM graph_rejection_drafts WHERE thread_id = ?", (thread_id,))
                db.execute("DELETE FROM graph_review_owners WHERE thread_id = ?", (thread_id,))
        except Exception:
            logger.exception("Could not remove owner record after review start failed")
        if isinstance(exc, AgentError):
            raise HTTPException(status_code=403, detail='HR access required') from exc
        raise


@router.get("/{thread_id}")
def read_review(thread_id: str, request: Request, user: HR):
    path = request.app.state.database_path
    initialize_graph_reviews(path)
    _owner(path, thread_id, user)
    with SqliteSaver.from_conn_string(str(path)) as saver:
        graph = build_review_graph(path, saver)
        return {"id": thread_id, **_view(graph.get_state(_config(thread_id)), path, request=request, user=user)}


@router.post("/{thread_id}/resume")
def resume_review(thread_id: str, payload: ReviewReply, request: Request, user: HR):
    path = request.app.state.database_path
    initialize_graph_reviews(path)
    _owner(path, thread_id, user)
    with _review_lock(path, thread_id), closing(sqlite3.connect(str(path), timeout=10, check_same_thread=False)) as db:
        db.row_factory = sqlite3.Row
        saver = _AtomicResumeSaver(db)
        saver.setup()
        graph = build_review_graph(path, saver, user=user, action_db=db)
        config = _config(thread_id)
        db.execute('BEGIN IMMEDIATE')
        try:
            _authorize_resume(db, request, user, thread_id)
            snapshot = graph.get_state(config)
            if not snapshot.next:
                raise HTTPException(status_code=409, detail="Review is not awaiting input")
            if ((snapshot.values.get('intent') in ('semantic_review', 'semantic_advance_and_reserve_slot') or
                    (snapshot.values.get('intent') == 'adaptive' and 'semantic' in snapshot.values.get('plan', []))) and payload.reviewed and
                    restore_references(db, snapshot.values['application_id'], snapshot.values.get('semantic_refs'))['status'] != 'current'):
                raise HTTPException(status_code=409, detail='Resume or criteria changed; start a new semantic review')
            if (snapshot.values.get('intent') == 'adaptive' and
                    context_for(db, snapshot.values['application_id'])[1] != snapshot.values.get('context_digest')):
                raise HTTPException(status_code=409, detail='Application changed; start a new review')
            if (snapshot.values.get('intent') == 'adaptive' and 'policy' in snapshot.values.get('plan', [])
                    and payload.reviewed and not _adaptive_policy_current(db, snapshot.values.get('policy'))):
                raise HTTPException(status_code=409, detail='Policy sources changed; start a new review')
            if (snapshot.values.get('intent') in ('create_slot', 'create_private_slot', 'advance_application', 'advance_and_reserve_slot', 'semantic_advance_and_reserve_slot', 'screen_application', 'complete_interview', 'present_offer', 'finalize_hire', 'reject_application') or snapshot.values.get('adaptive_goal') in ('schedule_interview', 'advance_and_reserve')) and payload.reviewed:
                expected = snapshot.values.get('action_proposal', {}).get('approval_fingerprint', '')
                if (not expected or not payload.approval_fingerprint or
                        not secrets.compare_digest(expected.encode('utf-8'), payload.approval_fingerprint.encode('utf-8'))):
                    raise HTTPException(status_code=409, detail='Approval must match the displayed proposal')
            graph.invoke(Command(resume=payload.reviewed), config)
            # Time can advance while the SQLite write lock is held.
            _authorize_resume(db, request, user, thread_id)
            db.commit()
        except BaseException:
            db.rollback()
            raise
        # Read the committed checkpoint with the normal saver; the transaction
        # is deliberately complete before _view opens its own connections.
        with SqliteSaver.from_conn_string(str(path)) as reader:
            graph = build_review_graph(path, reader)
            return {"id": thread_id, **_view(graph.get_state(config), path, request=request, user=user)}

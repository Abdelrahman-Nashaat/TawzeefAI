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
from app.graph_semantic import context as semantic_context
from app.graph_semantic import restore_references, save_references
from app.interview_guide import guide_from_matching
from app.matching_specialist import matching_snapshot
from app.policy_agent import authorized_policy_sources, run_policy_agent
from app.provisional_queue import (
    MAX_SOURCE_BYTES,
    MAX_SOURCE_CHARS,
    _assignment_revision,
    _bounded_job,
    _job_revision,
    _source_revisions,
    provisional_application_state,
)
from app.recruitment import (
    _require_current_role,
    get_application,
    score_masked_resume,
    store_screening,
)
from app.resume_fields import resume_fields_for_display
from app.resume_mask import masked_source
from app.resume_structure import structure_resume
from app.scheduling import utc_text
from app.slot_reservations import private_slot_conflict
from app.slots import BatchSlot, SlotCreate, _validate_batch, create_slot_record
from app.workflow import candidate_stage

router = APIRouter(prefix="/api/graph-reviews")
HR = Annotated[User, Depends(require_roles("hr"))]
logger = logging.getLogger(__name__)
_matching_workers = BoundedSemaphore(2)


class ReviewRequest(BaseModel):
    application_id: int = Field(gt=0)
    policy_question: str = Field(default="", max_length=2000)
    intent: Literal['full', 'policy', 'scheduling', 'status', 'create_slot', 'create_private_slot', 'advance_application', 'advance_and_reserve_slot', 'semantic_review', 'semantic_advance_and_reserve_slot', 'adaptive', 'screen_application'] = 'full'
    slot: SlotCreate | None = None
    adaptive_goal: Literal['review_application', 'schedule_interview', 'advance_and_reserve'] | None = None
    allow_semantic: StrictBool = False

    @model_validator(mode='before')
    @classmethod
    def restrict_adaptive_input(cls, data):
        if (isinstance(data, dict) and data.get('intent') == 'adaptive' and
                set(data) - {'application_id', 'intent', 'adaptive_goal', 'slot', 'allow_semantic'}):
            raise ValueError('Adaptive review accepts no tools, identity overrides, or extra fields')
        return data

    @model_validator(mode='after')
    def check_policy_question(self):
        if self.intent == 'policy' and not self.policy_question.strip():
            raise ValueError('Policy review requires a question')
        if self.intent in ('scheduling', 'status', 'advance_application', 'advance_and_reserve_slot', 'semantic_review', 'semantic_advance_and_reserve_slot', 'screen_application') and self.policy_question.strip():
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
                 'message': 'Graph matching supports resumes up to 4,000 characters and at most 20 job skills of 80 characters each. Review larger inputs manually.'}
    if row['source_bytes'] > MAX_SOURCE_BYTES:
        raise HTTPException(status_code=422, detail=too_large)
    raw = db.execute('SELECT resume_text FROM applications WHERE id=?', (application_id,)).fetchone()[0]
    if len(raw) > MAX_SOURCE_CHARS:
        raise HTTPException(status_code=422, detail=too_large)
    try:
        job = _bounded_job(db, row['job_id'])
    except HTTPException as exc:
        if exc.status_code == 422:
            raise HTTPException(status_code=422, detail=too_large) from exc
        raise
    return row['job_id'], job, masked_source(db, application_id)


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
        ]}}

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
                        'candidate_id': application['candidate_id'], 'current_status': 'submitted',
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
        notice = ('Approve recording this keyword screening. No hiring decision or message is made.'
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
             "prepare_screen": prepare_screen, "execute_screen": execute_screen}
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
        builder.add_node(name, guarded(node) if name in {'parser', 'matching', 'semantic', 'status', 'scheduler', 'prepare_slot'} else node)
    builder.add_edge(START, 'plan')
    def next_after(name):
        def route(state: ReviewState):
            steps = state['plan']
            return steps[0] if name == 'plan' else steps[steps.index(name) + 1]
        return route
    choices = ['parser', 'matching', 'semantic', 'policy', 'scheduler', 'status', 'agent_review', 'prepare_slot', 'prepare_advance', 'prepare_screen', 'human_review', 'execute_slot', 'execute_advance', 'execute_screen']
    for name in ['plan', 'parser', 'matching', 'semantic', 'policy', 'scheduler', 'status', 'agent_review', 'prepare_slot', 'prepare_advance', 'prepare_screen']:
        builder.add_conditional_edges(name, next_after(name), choices)
    builder.add_conditional_edges('human_review', lambda state: 'execute_screen' if state.get('intent') == 'screen_application' else 'execute_advance' if state.get('intent') in ('advance_application', 'advance_and_reserve_slot', 'semantic_advance_and_reserve_slot') or state.get('adaptive_goal') == 'advance_and_reserve' else 'execute_slot' if state.get('intent') in ('create_slot', 'create_private_slot') or state.get('adaptive_goal') == 'schedule_interview' else END,
                                  {'execute_slot': 'execute_slot', 'execute_advance': 'execute_advance', 'execute_screen': 'execute_screen', END: END})
    builder.add_edge('execute_slot', END)
    builder.add_edge('execute_advance', END)
    builder.add_edge('execute_screen', END)
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
    parser_ref = state.pop('parser_source_ref', None)
    parser_context_ref = state.pop('parser_context_ref', parser_ref)
    semantic_refs = state.pop('semantic_refs', None)
    # Old checkpoints lack intent and retain the historical full-review view.
    # Never expose stale stage data outside an explicitly selected scope.
    selected = state.get('intent', 'full')
    scoped_fields = {'parsed': 'full', 'matching': 'full', 'agent_review': 'full',
                     'policy': 'policy', 'scheduling': 'scheduling', 'status': 'status'}
    if selected not in {'full', 'policy', 'scheduling', 'status', 'create_slot', 'create_private_slot', 'advance_application', 'advance_and_reserve_slot', 'semantic_review', 'semantic_advance_and_reserve_slot', 'adaptive', 'screen_application'}:
        selected = 'invalid'
    for field, scope in scoped_fields.items():
        adaptive_fields = {'parsed': 'parser', 'matching': 'matching', 'scheduling': 'scheduler', 'status': 'status'}
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
                        state['policy'] = {'status': 'answer',
                            'answer': '\n\n'.join(f'[{s.id}] {s.text}' for s in sources),
                            'sources': [asdict(source) for source in sources],
                            'notice': 'Extractive excerpts; review the source for relevance.'}
            application = get_application(db, state["application_id"])
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
        db.execute("INSERT INTO graph_review_owners (thread_id, application_id, owner_id) VALUES (?, ?, ?)",
                   (thread_id, payload.application_id, user.id))
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
            if (snapshot.values.get('intent') in ('create_slot', 'create_private_slot', 'advance_application', 'advance_and_reserve_slot', 'semantic_advance_and_reserve_slot', 'screen_application') or snapshot.values.get('adaptive_goal') in ('schedule_interview', 'advance_and_reserve')) and payload.reviewed:
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

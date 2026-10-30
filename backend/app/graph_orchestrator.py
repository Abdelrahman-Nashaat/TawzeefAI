"""A local, human-supervised LangGraph review; it never changes hiring data.

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
import logging
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Annotated, TypedDict

from fastapi import APIRouter, Depends, HTTPException, Request
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from app.agent import AgentError, run_agent
from app.auth import COOKIE_NAME, User, get_current_user, require_roles, token_digest
from app.blind import redact_resume
from app.conversations import _authorized_sources, _digest
from app.db import connect
from app.knowledge import answer_question
from app.recruitment import get_application, job_data, score_resume
from app.resume_structure import structure_resume
from app.scheduling import utc_text

router = APIRouter(prefix="/api/graph-reviews")
HR = Annotated[User, Depends(require_roles("hr"))]
logger = logging.getLogger(__name__)


class ReviewRequest(BaseModel):
    application_id: int = Field(gt=0)
    policy_question: str = Field(default="", max_length=2000)


class ReviewReply(BaseModel):
    reviewed: bool


class ReviewState(TypedDict, total=False):
    application_id: int
    plan: list[str]
    parsed: dict
    matching: dict
    policy: dict
    scheduling: dict
    status: dict
    agent_review: dict
    reviewed: bool


def initialize_graph_reviews(path: str | Path) -> None:
    with connect(path) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS graph_review_owners (
            thread_id TEXT PRIMARY KEY,
            application_id INTEGER NOT NULL REFERENCES applications(id),
            owner_id INTEGER NOT NULL REFERENCES users(id)
        )""")


def _current_scheduling(path: str | Path, application_id: int) -> dict:
    with connect(path) as db:
        application = get_application(db, application_id)
        if application["status"] != "advanced":
            return {"available_slots": [], "eligible": False,
                    "notice": "Application is not eligible for candidate booking."}
        if db.execute("SELECT 1 FROM interviews WHERE application_id = ? AND status = 'scheduled'",
                      (application_id,)).fetchone():
            return {"available_slots": [], "eligible": False,
                    "notice": "Application already has a scheduled interview."}
        rows = db.execute("""SELECT s.id, s.start_at, s.end_at FROM interview_slots s
            LEFT JOIN interviews linked ON linked.id = s.interview_id
            WHERE s.withdrawn = 0 AND s.start_at > ? AND (s.interview_id IS NULL OR linked.status = 'cancelled')
            AND NOT EXISTS (SELECT 1 FROM interviews i WHERE i.status = 'scheduled'
                AND i.start_at < s.end_at AND i.end_at > s.start_at
                AND (i.candidate_id = ? OR i.interviewer_key = s.interviewer_key))
            ORDER BY s.start_at, s.id LIMIT 10""",
            (utc_text(datetime.now(UTC)), application["candidate_id"])).fetchall()
    return {"available_slots": [dict(r) for r in rows], "eligible": True,
            "notice": "Availability is a snapshot; booking remains a separate action."}


def build_review_graph(path: str | Path, checkpointer, *, policy_question: str = "",
                       user: User | None = None, authorize=None, enable_agent: bool = False):
    """Compile a bounded graph around existing, read-only domain operations."""
    def plan(state: ReviewState):
        return {"plan": ["parser", "matching", "policy", "scheduler", "status", "agent_review", "human_review"]}

    def parser(state: ReviewState):
        with connect(path) as db:
            row = get_application(db, state["application_id"])
            text = row["resume_text"]
        result = structure_resume(redact_resume(text).text)
        return {"parsed": {"text_available": bool(text.strip()),
                           "sections": {name: section["status"] for name, section in result["sections"].items()},
                           "notice": "Section mentions are unverified; redaction is incomplete. "
                                     "The structured résumé endpoint exposes text for HR review."}}

    def matching(state: ReviewState):
        with connect(path) as db:
            row = get_application(db, state["application_id"])
            job = db.execute("SELECT * FROM jobs WHERE id = ?", (row["job_id"],)).fetchone()
            score = score_resume(job_data(job), row["resume_text"])
            # Evidence excerpts can include identifying data even after masking.
            for group in ("required", "preferred"):
                score[group] = [{"skill": item["skill"], "mentioned": item["mentioned"]}
                                for item in score[group]]
            return {"matching": score}

    def policy(state: ReviewState):
        question = policy_question.strip()
        if not question:
            return {"policy": {"status": "not_requested"}}
        answer = answer_question(question, "hr", database_path=path, enable_groq=False)
        if answer.mode == 'abstain' and not answer.sources:
            return {"policy": {"status": "abstain"}}
        return {"policy": {"status": "source_refs_v1", "refs": [
            {"id": source.id, "digest": _digest(source)} for source in answer.sources
        ]}}

    def scheduler(state: ReviewState):
        return {"scheduling": _current_scheduling(path, state["application_id"])}

    def status(state: ReviewState):
        with connect(path) as db:
            row = get_application(db, state["application_id"])
            return {"status": {"application_status": row["status"]}}

    def agent_review(state: ReviewState):
        if not enable_agent or user is None:
            return {"agent_review": {"status": "disabled", "tool_trace": []}}
        result = run_agent(path, state["application_id"], user, 'prepare review',
                           enabled=True, authorize=authorize)
        # Never persist model output or tool payloads in the graph checkpoint.
        return {"agent_review": {"status": result['status'], "tool_trace": result['tool_trace']}}

    def human_review(state: ReviewState):
        reviewed = interrupt({"type": "human_review", "application_id": state["application_id"],
                              "notice": "Acknowledge review; no hiring or booking action will be taken."})
        return {"reviewed": reviewed}

    builder = StateGraph(ReviewState)
    nodes = {"plan": plan, "parser": parser, "matching": matching, "policy": policy,
             "scheduler": scheduler, "status": status, "agent_review": agent_review,
             "human_review": human_review}
    for name, node in nodes.items():
        builder.add_node(name, node)
    chain = [START, *nodes, END]
    for first, second in pairwise(chain):
        builder.add_edge(first, second)
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
                if policy.get('status') == 'source_refs_v1':
                    refs = policy.get('refs')
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
            state["status"] = {"application_status": application["status"]}
        state["scheduling"] = _current_scheduling(path, state["application_id"])
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
                                       enable_agent=os.environ.get('TAWZEEF_ENABLE_GROQ') == '1')
            config = _config(thread_id)
            graph.invoke({"application_id": payload.application_id}, config)
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
    with _review_lock(path, thread_id), SqliteSaver.from_conn_string(str(path)) as saver:
        graph = build_review_graph(path, saver)
        config = _config(thread_id)
        if not graph.get_state(config).next:
            raise HTTPException(status_code=409, detail="Review is not awaiting input")
        graph.invoke(Command(resume=payload.reviewed), config)
        return {"id": thread_id, **_view(graph.get_state(config), path, request=request, user=user)}

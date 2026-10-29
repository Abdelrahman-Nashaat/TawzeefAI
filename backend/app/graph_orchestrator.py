"""A local, human-supervised LangGraph review; it never changes hiring data.

Each review belongs to the authenticated HR user who started it. SQLite
checkpoints contain the review results (including potentially sensitive CV
evidence), so the database must be protected like the recruitment database.
The graph uses existing local tools; it does not make an LLM call or send mail.
Policy retrieval is metadata-only: neither the question nor search results are
saved or returned by these endpoints. Use the knowledge endpoint for answers.
Old checkpoint payloads are redacted from responses, but may still exist on disk.
"""

import errno
import hashlib
import logging
import os
import secrets
from contextlib import contextmanager
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Annotated, TypedDict

from fastapi import APIRouter, Depends, HTTPException, Request
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from app.auth import User, require_roles
from app.blind import redact_resume
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
            WHERE s.start_at > ? AND (s.interview_id IS NULL OR linked.status = 'cancelled')
            AND NOT EXISTS (SELECT 1 FROM interviews i WHERE i.status = 'scheduled'
                AND i.start_at < s.end_at AND i.end_at > s.start_at
                AND (i.candidate_id = ? OR i.interviewer_key = s.interviewer_key))
            ORDER BY s.start_at, s.id LIMIT 10""",
            (utc_text(datetime.now(UTC)), application["candidate_id"])).fetchall()
    return {"available_slots": [dict(r) for r in rows], "eligible": True,
            "notice": "Availability is a snapshot; booking remains a separate action."}


def build_review_graph(path: str | Path, checkpointer, *, policy_question: str = ""):
    """Compile a bounded graph around existing, read-only domain operations."""
    def plan(state: ReviewState):
        return {"plan": ["parser", "matching", "policy", "scheduler", "status", "human_review"]}

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
        # The input and result never enter graph state or checkpoints. In
        # particular, a saved answer cannot outlive access to its sources.
        question = policy_question.strip()
        if not question:
            return {"policy": {"status": "not_requested"}}
        answer_question(question, "hr", database_path=path, enable_groq=False)
        return {"policy": {"status": "searched_without_retaining_results"}}

    def scheduler(state: ReviewState):
        return {"scheduling": _current_scheduling(path, state["application_id"])}

    def status(state: ReviewState):
        with connect(path) as db:
            row = get_application(db, state["application_id"])
            return {"status": {"application_status": row["status"]}}

    def human_review(state: ReviewState):
        reviewed = interrupt({"type": "human_review", "application_id": state["application_id"],
                              "notice": "Acknowledge review; no hiring or booking action will be taken."})
        return {"reviewed": reviewed}

    builder = StateGraph(ReviewState)
    nodes = {"plan": plan, "parser": parser, "matching": matching, "policy": policy,
             "scheduler": scheduler, "status": status, "human_review": human_review}
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


def _view(snapshot, path: str | Path | None = None) -> dict:
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
    if "policy" in state:
        status = state["policy"].get("status")
        state["policy"] = {"status": status if status in {
            "not_requested", "searched_without_retaining_results"
        } else "unavailable"}
    if path is not None and "application_id" in state:
        with connect(path) as db:
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
            graph = build_review_graph(path, saver, policy_question=payload.policy_question)
            config = _config(thread_id)
            graph.invoke({"application_id": payload.application_id}, config)
            return {"id": thread_id, **_view(graph.get_state(config), path)}
    except Exception:
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
        raise


@router.get("/{thread_id}")
def read_review(thread_id: str, request: Request, user: HR):
    path = request.app.state.database_path
    initialize_graph_reviews(path)
    _owner(path, thread_id, user)
    with SqliteSaver.from_conn_string(str(path)) as saver:
        graph = build_review_graph(path, saver)
        return {"id": thread_id, **_view(graph.get_state(_config(thread_id)), path)}


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
        return {"id": thread_id, **_view(graph.get_state(config), path)}

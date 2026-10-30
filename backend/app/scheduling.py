"""Local interview appointments; this does not connect an external calendar."""

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
from app.google_outbox import cancellation_block_reason

router = APIRouter(prefix="/api")
HR = Annotated[User, Depends(require_roles("hr"))]
Authenticated = Annotated[User, Depends(get_current_user)]


def initialize_scheduling(path: str | Path) -> None:
    with connect(path) as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS interviews (
                id INTEGER PRIMARY KEY,
                application_id INTEGER NOT NULL REFERENCES applications(id),
                candidate_id INTEGER NOT NULL REFERENCES users(id),
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                interviewer TEXT NOT NULL,
                interviewer_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'cancelled')),
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            CREATE INDEX IF NOT EXISTS interview_candidate ON interviews(candidate_id, start_at, end_at);
            CREATE INDEX IF NOT EXISTS interview_interviewer ON interviews(interviewer_key, start_at, end_at);
            CREATE TABLE IF NOT EXISTS interview_completions (
                interview_id INTEGER PRIMARY KEY REFERENCES interviews(id),
                recorded_by INTEGER NOT NULL REFERENCES users(id),
                recorded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );
            """
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(interviews)")}
        if "status" not in columns:
            db.execute("ALTER TABLE interviews ADD COLUMN status TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'cancelled'))")


class InterviewCreate(BaseModel):
    application_id: int = Field(gt=0)
    start_at: datetime
    end_at: datetime
    interviewer: str = Field(min_length=1, max_length=120)

    @field_validator("start_at", "end_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("An explicit timezone offset is required")
        return value.astimezone(UTC)

    @field_validator("interviewer")
    @classmethod
    def clean_interviewer(cls, value: str) -> str:
        value = " ".join(unicodedata.normalize("NFKC", value).split())
        if not value:
            raise ValueError("Interviewer is required")
        return value

    @model_validator(mode="after")
    def validate_range(self):
        if self.end_at <= self.start_at:
            raise ValueError("Interview end must be after its start")
        return self


def utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def interview_view(row) -> dict:
    return {**{key: row[key] for key in ("id", "application_id", "start_at", "end_at", "interviewer", "status", "created_at")},
            "completed": bool(row["completed"]) if "completed" in set(row.keys()) else False}


def _require_current_hr(db, request: Request, user: User) -> None:
    session = db.execute("""SELECT u.username,u.role FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.user_id=? AND s.token_hash=? AND s.expires_at>? AND u.active=1""",
        (user.id, token_digest(request.cookies.get(COOKIE_NAME, '')), int(time.time()))).fetchone()
    if session is None or session['username'] != user.username:
        raise HTTPException(status_code=401, detail="Authentication required")
    if session['role'] != 'hr':
        raise HTTPException(status_code=403, detail="HR access required")


@router.post("/interviews/{interview_id}/complete")
def complete_interview(interview_id: int, request: Request, user: HR):
    """Record that HR confirms an interview took place; no provider calls occur."""
    with connect(request.app.state.database_path) as db:
        db.execute("BEGIN IMMEDIATE")
        _require_current_hr(db, request, user)
        row = db.execute("""SELECT i.*, a.status AS application_status FROM interviews i
            JOIN applications a ON a.id=i.application_id WHERE i.id=?""", (interview_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Interview not found")
        if row['status'] != 'scheduled' or row['application_status'] not in ('submitted', 'advanced'):
            raise HTTPException(status_code=409, detail="Interview cannot be marked completed")
        try:
            end = datetime.fromisoformat(row['end_at'])
            if end.tzinfo is None or end.utcoffset() is None or end > datetime.now(UTC):
                raise ValueError('Interview is not over')
        except (TypeError, ValueError, OverflowError) as exc:
            raise HTTPException(status_code=409, detail="Interview end must have passed") from exc
        if db.execute("SELECT 1 FROM interview_completions WHERE interview_id=?", (interview_id,)).fetchone():
            raise HTTPException(status_code=409, detail="Interview completion already recorded")
        db.execute("INSERT INTO interview_completions(interview_id, recorded_by) VALUES (?,?)", (interview_id, user.id))
        db.execute("INSERT INTO audit_events(application_id,actor_id,action,details) VALUES (?,?,?,?)",
                   (row['application_id'], user.id, 'interview_completed', json.dumps({'interview_id': interview_id})))
        updated = db.execute("SELECT *, 1 AS completed FROM interviews WHERE id=?", (interview_id,)).fetchone()
        _require_current_hr(db, request, user)
        return interview_view(updated)


@router.post("/interviews", status_code=201)
def create_interview(payload: InterviewCreate, request: Request, user: HR):
    start = utc_text(payload.start_at)
    end = utc_text(payload.end_at)
    interviewer_key = payload.interviewer.casefold()
    with connect(request.app.state.database_path) as db:
        # Serialize conflict checking and insertion across concurrent requests.
        db.execute("BEGIN IMMEDIATE")
        if payload.start_at <= datetime.now(UTC):
            raise HTTPException(status_code=422, detail="Interview start must be in the future")
        application = db.execute("SELECT candidate_id, status FROM applications WHERE id = ?", (payload.application_id,)).fetchone()
        if application is None:
            raise HTTPException(status_code=404, detail="Application not found")
        if application["status"] not in {"submitted", "advanced"}:
            raise HTTPException(status_code=409, detail="Cannot schedule a finalized application")
        if db.execute("""SELECT 1 FROM interviews i WHERE i.application_id = ? AND i.status = 'scheduled'
                      AND NOT EXISTS (SELECT 1 FROM interview_completions c WHERE c.interview_id=i.id) LIMIT 1""",
                      (payload.application_id,)).fetchone():
            raise HTTPException(status_code=409, detail="Application already has an interview")
        conflict = db.execute(
            """SELECT 1 FROM interviews
               WHERE status = 'scheduled' AND start_at < ? AND end_at > ? AND (candidate_id = ? OR interviewer_key = ?)
               LIMIT 1""",
            (end, start, application["candidate_id"], interviewer_key),
        ).fetchone()
        if conflict:
            raise HTTPException(status_code=409, detail="Candidate or interviewer has a conflicting interview")
        cursor = db.execute(
            """INSERT INTO interviews (application_id, candidate_id, start_at, end_at, interviewer, interviewer_key, created_by)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (payload.application_id, application["candidate_id"], start, end, payload.interviewer, interviewer_key, user.id),
        )
        db.execute(
            "INSERT INTO audit_events (application_id, actor_id, action, details) VALUES (?, ?, ?, ?)",
            (payload.application_id, user.id, "interview_scheduled", json.dumps({"interview_id": cursor.lastrowid})),
        )
        row = db.execute("SELECT * FROM interviews WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return interview_view(row)


@router.post("/interviews/{interview_id}/cancel")
def cancel_interview(interview_id: int, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM interviews WHERE id = ?", (interview_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Interview not found")
        if row["status"] != "scheduled":
            raise HTTPException(status_code=409, detail="Interview is already cancelled")
        if row["start_at"] <= utc_text(datetime.now(UTC)):
            raise HTTPException(status_code=409, detail="Cannot cancel a past or in-progress interview")
        if cancellation_block_reason(db, interview_id):
            raise HTTPException(status_code=409, detail="Resolve the Google delivery before cancelling this interview")
        db.execute("UPDATE interviews SET status = 'cancelled' WHERE id = ?", (interview_id,))
        db.execute(
            "INSERT INTO audit_events (application_id, actor_id, action, details) VALUES (?, ?, ?, ?)",
            (row["application_id"], user.id, "interview_cancelled", json.dumps({"interview_id": interview_id, "reason": "hr_cancelled"})),
        )
        return interview_view(db.execute("SELECT * FROM interviews WHERE id = ?", (interview_id,)).fetchone())


@router.get("/interviews")
def list_interviews(request: Request, user: Authenticated):
    with connect(request.app.state.database_path) as db:
        if user.role == "hr":
            rows = db.execute("SELECT i.*, EXISTS(SELECT 1 FROM interview_completions c WHERE c.interview_id=i.id) AS completed FROM interviews i ORDER BY start_at, id").fetchall()
        elif user.role == "candidate":
            rows = db.execute("SELECT i.*, EXISTS(SELECT 1 FROM interview_completions c WHERE c.interview_id=i.id) AS completed FROM interviews i WHERE candidate_id = ? ORDER BY start_at, id", (user.id,)).fetchall()
        else:
            raise HTTPException(status_code=403, detail="Access denied")
        return [interview_view(row) for row in rows]

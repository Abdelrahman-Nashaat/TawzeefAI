"""Local interview appointments; this does not connect an external calendar."""

import json
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from app.auth import User, get_current_user, require_roles
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
    return {key: row[key] for key in ("id", "application_id", "start_at", "end_at", "interviewer", "status", "created_at")}


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
            rows = db.execute("SELECT * FROM interviews ORDER BY start_at, id").fetchall()
        elif user.role == "candidate":
            rows = db.execute("SELECT * FROM interviews WHERE candidate_id = ? ORDER BY start_at, id", (user.id,)).fetchall()
        else:
            raise HTTPException(status_code=403, detail="Access denied")
        return [interview_view(row) for row in rows]

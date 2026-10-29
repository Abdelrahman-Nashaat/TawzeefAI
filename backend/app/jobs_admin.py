"""HR creation of local jobs, with validation and an atomic audit event."""

import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth import User, require_roles
from app.db import connect

router = APIRouter(prefix='/api')
HR = Annotated[User, Depends(require_roles('hr'))]


def initialize_jobs_admin(path: str | Path) -> None:
    with connect(path) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS job_audit_events (
            id INTEGER PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES jobs(id),
            actor_id INTEGER NOT NULL REFERENCES users(id),
            action TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        )''')


def _clean_item(value: str, *, limit: int = 200) -> str:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('Control characters are not allowed')
    cleaned = ' '.join(value.split())
    if not cleaned or len(cleaned) > limit:
        raise ValueError('Value must be non-empty and within the length limit')
    return cleaned


class JobCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    id: str = Field(min_length=1, max_length=64, pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
    title: str = Field(min_length=1, max_length=200)
    location: str = Field(min_length=1, max_length=200)
    required_skills: list[str] = Field(min_length=1, max_length=50)
    preferred_skills: list[str] = Field(max_length=50)
    responsibilities: list[str] = Field(min_length=1, max_length=50)

    @field_validator('title', 'location')
    @classmethod
    def clean_text(cls, value: str) -> str:
        return _clean_item(value)

    @field_validator('required_skills', 'preferred_skills', 'responsibilities')
    @classmethod
    def clean_entries(cls, value: list[str]) -> list[str]:
        return [_clean_item(item) for item in value]


@router.post('/jobs', status_code=201)
def create_job(payload: JobCreate, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        # Serialize the conflict check with insertion and the audit event.
        db.execute('BEGIN IMMEDIATE')
        if db.execute('SELECT 1 FROM jobs WHERE id = ?', (payload.id,)).fetchone():
            raise HTTPException(status_code=409, detail='Job identifier already exists')
        db.execute(
            '''INSERT INTO jobs (id, title, location, required_skills, preferred_skills, responsibilities)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (payload.id, payload.title, payload.location,
             json.dumps(payload.required_skills, ensure_ascii=False),
             json.dumps(payload.preferred_skills, ensure_ascii=False),
             json.dumps(payload.responsibilities, ensure_ascii=False)),
        )
        db.execute(
            "INSERT INTO job_audit_events (job_id, actor_id, action) VALUES (?, ?, 'created')",
            (payload.id, user.id),
        )
        return payload.model_dump()

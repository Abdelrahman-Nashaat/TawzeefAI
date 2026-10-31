"""Versioned HR review of exact spans in a partially redacted résumé.

Values are extracted from the current source, never accepted from the client.
This records human selections, not verified qualifications or hiring decisions.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.auth import User, require_roles
from app.blind import redact_resume
from app.db import connect
from app.recruitment import _require_current_role, get_application
from app.resume_fields import _FIELDS, MAX_FIELD_CHARS, MAX_SOURCE_CHARS

router = APIRouter(prefix='/api')
HR = Annotated[User, Depends(require_roles('hr'))]
_MASKED = re.compile(r'\[(?:identity field|email|phone|link) removed\]')


def initialize_resume_field_reviews(path: str | Path) -> None:
    with connect(path) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS resume_field_reviews (
            id INTEGER PRIMARY KEY,
            application_id INTEGER NOT NULL REFERENCES applications(id),
            version INTEGER NOT NULL CHECK(version > 0),
            source_sha256 TEXT NOT NULL,
            fields_json TEXT NOT NULL,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE(application_id, version)
        )''')


class ReviewedField(BaseModel):
    model_config = ConfigDict(extra='forbid')
    record_ref: str = Field(pattern=r'^r-[A-Za-z0-9_-]{1,64}$')
    section: Literal['experience', 'education', 'projects']
    field: str = Field(min_length=1, max_length=32)
    start: StrictInt = Field(ge=0)
    end: StrictInt = Field(gt=0)

    @model_validator(mode='after')
    def valid_field_and_span(self):
        if self.field not in _FIELDS[self.section]:
            raise ValueError('Unsupported field for section')
        if self.end <= self.start or self.end - self.start > MAX_FIELD_CHARS:
            raise ValueError('Invalid field span')
        return self


class ReviewSave(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    expected_version: StrictInt = Field(ge=0)
    fields: list[ReviewedField] = Field(max_length=300)

    @model_validator(mode='after')
    def validate_grouping(self):
        groups = {}
        seen = set()
        for item in self.fields:
            if item.record_ref in groups and groups[item.record_ref] != item.section:
                raise ValueError('A record cannot cross sections')
            groups[item.record_ref] = item.section
            key = (item.record_ref, item.field)
            if key in seen:
                raise ValueError('Duplicate field in record')
            seen.add(key)
        if len(groups) > 100:
            raise ValueError('Too many records')
        return self


def _source(db, application_id: int) -> tuple[str, str]:
    application = get_application(db, application_id)
    source = redact_resume(application['resume_text']).text
    if len(source) > MAX_SOURCE_CHARS:
        raise HTTPException(status_code=422, detail='Redacted resume exceeds the review limit')
    return source, hashlib.sha256(source.encode('utf-8')).hexdigest()


def _latest(db, application_id: int):
    return db.execute('''SELECT version, source_sha256, fields_json, created_by, created_at
        FROM resume_field_reviews WHERE application_id=? ORDER BY version DESC LIMIT 1''',
        (application_id,)).fetchone()


def _values(fields: list[ReviewedField], source: str) -> list[dict]:
    masked_spans = [(m.start(), m.end()) for m in _MASKED.finditer(source)]
    result = []
    for item in fields:
        if item.end > len(source) or not source[item.start:item.end].strip():
            raise ValueError('Field span is outside the source or empty')
        if any(item.start < end and item.end > start for start, end in masked_spans):
            raise ValueError('Field span overlaps removed identity information')
        result.append({**item.model_dump(), 'value': source[item.start:item.end]})
    return result


def _view(row, source: str, digest: str) -> dict:
    result = {'source_kind': 'partially_redacted_resume', 'offset_unit': 'unicode_code_points',
              'source': source, 'source_sha256': digest, 'version': row['version'] if row else 0,
              'status': 'not_reviewed', 'fields': [],
              'notice': 'Human-selected spans are not verified facts; redaction may be incomplete.'}
    if row is None:
        return result
    result.update({'reviewed_by': row['created_by'], 'reviewed_at': row['created_at']})
    if row['source_sha256'] != digest:
        result['status'] = 'stale'
        return result
    try:
        saved = ReviewSave.model_validate({'source_sha256': digest, 'expected_version': row['version'],
                                           'fields': json.loads(row['fields_json'])})
        result.update({'status': 'current', 'fields': _values(saved.fields, source)})
    except (ValueError, TypeError, KeyError):
        result['status'] = 'stale'
    return result


@router.get('/applications/{application_id}/resume/field-review')
def read_field_review(application_id: int, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        source, digest = _source(db, application_id)
        result = _view(_latest(db, application_id), source, digest)
        _require_current_role(db, request, user, 'hr')
        return result


@router.put('/applications/{application_id}/resume/field-review')
def save_field_review(application_id: int, payload: ReviewSave, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        source, digest = _source(db, application_id)
        previous = _latest(db, application_id)
        version = previous['version'] if previous else 0
        if payload.source_sha256 != digest or payload.expected_version != version:
            raise HTTPException(status_code=409, detail='Resume or review changed; reload before saving')
        try:
            _values(payload.fields, source)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail='Invalid selection in redacted resume') from exc
        fields_json = json.dumps([item.model_dump() for item in payload.fields], ensure_ascii=False)
        db.execute('''INSERT INTO resume_field_reviews(application_id,version,source_sha256,fields_json,created_by)
                      VALUES (?,?,?,?,?)''', (application_id, version + 1, digest, fields_json, user.id))
        db.execute('''INSERT INTO audit_events(application_id,actor_id,action,details) VALUES (?,?,?,?)''',
                   (application_id, user.id, 'resume_fields_reviewed',
                    json.dumps({'version': version + 1, 'field_count': len(payload.fields)})))
        _require_current_role(db, request, user, 'hr')
        return _view(_latest(db, application_id), source, digest)

"""HR-managed, evidence-linked rubric assessments for a local demonstration.

Reviewers work against a partially redacted resume; this is not a guarantee of
anonymity. Neither a score nor an assessment changes an application decision.
"""

import json
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from app.auth import User, require_roles
from app.blind import redact_resume
from app.db import connect
from app.recruitment import get_application
from app.rubric import WEIGHTS, Assessment, calculate_rubric

router = APIRouter(prefix='/api')
HR = Annotated[User, Depends(require_roles('hr'))]
Category = Literal['mandatory', 'preferred', 'experience', 'projects', 'education']


def initialize_rubric_admin(path: str | Path) -> None:
    with connect(path) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS rubric_criteria (
            id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
            category TEXT NOT NULL CHECK(category IN ('mandatory','preferred','experience','projects','education')),
            label TEXT NOT NULL, created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE TABLE IF NOT EXISTS rubric_evidence (
            id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id),
            start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, excerpt TEXT NOT NULL,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE TABLE IF NOT EXISTS rubric_assessments (
            id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id),
            actor_id INTEGER NOT NULL REFERENCES users(id), assessment_json TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE TABLE IF NOT EXISTS rubric_audit_events (
            id INTEGER PRIMARY KEY, actor_id INTEGER NOT NULL REFERENCES users(id),
            action TEXT NOT NULL, job_id TEXT REFERENCES jobs(id),
            application_id INTEGER REFERENCES applications(id), reference_id INTEGER,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE INDEX IF NOT EXISTS rubric_criteria_job ON rubric_criteria(job_id);
        CREATE INDEX IF NOT EXISTS rubric_evidence_application ON rubric_evidence(application_id);
        CREATE INDEX IF NOT EXISTS rubric_assessments_application ON rubric_assessments(application_id, id);
        ''')


class CriterionCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    category: Category
    label: str = Field(min_length=1, max_length=200)

    @field_validator('label')
    @classmethod
    def normalize_label(cls, value: str) -> str:
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError('Control characters are not allowed')
        value = ' '.join(value.split())
        if not value:
            raise ValueError('A criterion label is required')
        if redact_resume(value).masked:
            raise ValueError('A criterion cannot contain detected identifying information')
        return value


class EvidenceCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start: int = Field(ge=0)
    end: int = Field(gt=0)

    @model_validator(mode='after')
    def validate_range(self):
        if self.end <= self.start or self.end - self.start > 500:
            raise ValueError('Evidence must span 1 to 500 characters')
        return self


class Judgment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    criterion_ref: str = Field(pattern=r'^c-[1-9][0-9]{0,3}$')
    status: Literal['met', 'missing', 'unknown']
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)
    absence_review_attested: StrictBool = False

    @field_validator('evidence_refs')
    @classmethod
    def validate_evidence_refs(cls, refs: list[str]) -> list[str]:
        import re
        if len(refs) != len(set(refs)) or any(not re.fullmatch(r'e-[1-9][0-9]{0,3}', ref) for ref in refs):
            raise ValueError('Invalid or duplicated evidence references')
        return refs

    @model_validator(mode='after')
    def validate_status_evidence(self):
        if self.status == 'unknown' and self.evidence_refs:
            raise ValueError('Unknown judgments cannot claim evidence')
        if self.status != 'unknown' and not self.evidence_refs:
            raise ValueError('A reviewed judgment requires evidence')
        if self.status == 'missing' and not self.absence_review_attested:
            raise ValueError('Attest that you reviewed the available source and found no evidence for this criterion')
        if self.status != 'missing' and self.absence_review_attested:
            raise ValueError('Absence attestation applies only to missing judgments')
        return self


class AssessmentCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    judgments: list[Judgment] = Field(max_length=250)

    @model_validator(mode='after')
    def no_duplicates(self):
        refs = [j.criterion_ref for j in self.judgments]
        if len(refs) != len(set(refs)):
            raise ValueError('Duplicate criteria')
        return self


def require_job(db, job_id: str) -> None:
    if not db.execute('SELECT 1 FROM jobs WHERE id = ?', (job_id,)).fetchone():
        raise HTTPException(status_code=404, detail='Job not found')


def public_criterion(row) -> dict:
    return {'criterion_ref': f"c-{row['id']}", 'job_id': row['job_id'],
            'category': row['category'], 'label': row['label']}


def public_evidence(row) -> dict:
    return {'evidence_ref': f"e-{row['id']}", 'application_id': row['application_id'],
            'start': row['start_offset'], 'end': row['end_offset'], 'excerpt': row['excerpt']}


@router.get('/jobs/{job_id}/rubric/criteria')
def list_criteria(job_id: str, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        require_job(db, job_id)
        return [public_criterion(row) for row in db.execute(
            'SELECT * FROM rubric_criteria WHERE job_id = ? ORDER BY id', (job_id,))]


@router.post('/jobs/{job_id}/rubric/criteria', status_code=201)
def create_criterion(job_id: str, payload: CriterionCreate, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        require_job(db, job_id)
        if db.execute('SELECT COUNT(*) FROM rubric_criteria WHERE job_id = ? AND category = ?',
                      (job_id, payload.category)).fetchone()[0] >= 50:
            raise HTTPException(status_code=409, detail='Category criterion limit reached')
        cursor = db.execute('INSERT INTO rubric_criteria (job_id,category,label,created_by) VALUES (?,?,?,?)',
                            (job_id, payload.category, payload.label, user.id))
        if cursor.lastrowid > 9999:
            raise HTTPException(status_code=409, detail='Criterion reference limit reached')
        db.execute('INSERT INTO rubric_audit_events (actor_id,action,job_id,reference_id) VALUES (?, ?, ?, ?)',
                   (user.id, 'criterion_created', job_id, cursor.lastrowid))
        return public_criterion(db.execute('SELECT * FROM rubric_criteria WHERE id=?', (cursor.lastrowid,)).fetchone())


@router.get('/applications/{application_id}/rubric/source')
def blind_source(application_id: int, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        application = get_application(db, application_id)
        blind = redact_resume(application['resume_text'])
    return {'text': blind.text, 'requires_review': True, 'notice': blind.notice}


@router.get('/applications/{application_id}/rubric/evidence')
def list_evidence(application_id: int, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        get_application(db, application_id)
        return [public_evidence(row) for row in db.execute(
            'SELECT * FROM rubric_evidence WHERE application_id = ? ORDER BY id', (application_id,))]


@router.post('/applications/{application_id}/rubric/evidence', status_code=201)
def create_evidence(application_id: int, payload: EvidenceCreate, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        application = get_application(db, application_id)
        blind = redact_resume(application['resume_text'])
        if payload.end > len(blind.text):
            raise HTTPException(status_code=422, detail='Evidence range exceeds the source')
        excerpt = blind.text[payload.start:payload.end]
        if not excerpt.strip():
            raise HTTPException(status_code=422, detail='Evidence must contain visible text')
        cursor = db.execute('''INSERT INTO rubric_evidence
            (application_id,start_offset,end_offset,excerpt,created_by) VALUES (?,?,?,?,?)''',
            (application_id, payload.start, payload.end, excerpt, user.id))
        if cursor.lastrowid > 9999:
            raise HTTPException(status_code=409, detail='Evidence reference limit reached')
        db.execute('INSERT INTO rubric_audit_events (actor_id,action,application_id,reference_id) VALUES (?, ?, ?, ?)',
                   (user.id, 'evidence_created', application_id, cursor.lastrowid))
        return public_evidence(db.execute('SELECT * FROM rubric_evidence WHERE id=?', (cursor.lastrowid,)).fetchone())


@router.get('/applications/{application_id}/rubric/assessment')
def latest_assessment(application_id: int, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        application = get_application(db, application_id)
        row = db.execute('''SELECT id, actor_id, result_json, created_at FROM rubric_assessments
            WHERE application_id=? ORDER BY id DESC LIMIT 1''', (application_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='Assessment not found')
        result = json.loads(row['result_json'])
        # Older saved assessments had no explicit absence attestation. Treat
        # those judgments as unknown until a reviewer makes a fresh assessment.
        legacy_missing = False
        for component in result['components']:
            for claim in component['claims']:
                if claim['status'] == 'missing' and claim.get('absence_review_attested') is not True:
                    claim.update(status='unknown', evidence_refs=[], reviewed_by_hr=False,
                                 absence_review_attested=False)
                    legacy_missing = True
        if legacy_missing:
            result = calculate_rubric(Assessment.model_validate({
                component['category']: component['claims'] for component in result['components']
            }))
        assessed = {claim['criterion_ref'] for component in result['components'] for claim in component['claims']}
        current = {f"c-{item['id']}" for item in db.execute(
            'SELECT id FROM rubric_criteria WHERE job_id = ?', (application['job_id'],))}
        return {'id': row['id'], 'actor_id': row['actor_id'], 'created_at': row['created_at'],
                'result': result, 'outdated': legacy_missing or assessed != current,
                'requires_absence_review': legacy_missing}


@router.get('/rubric/audit')
def rubric_audit(request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        return [dict(row) for row in db.execute('SELECT * FROM rubric_audit_events ORDER BY id DESC')]


@router.post('/applications/{application_id}/rubric/assessment', status_code=201)
def assess(application_id: int, payload: AssessmentCreate, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        application = get_application(db, application_id)
        criteria = db.execute('SELECT * FROM rubric_criteria WHERE job_id = ? ORDER BY id',
                              (application['job_id'],)).fetchall()
        if not criteria:
            raise HTTPException(status_code=409, detail='No rubric criteria are configured for this job')
        known = {f"c-{row['id']}": row for row in criteria}
        judgments = {j.criterion_ref: j for j in payload.judgments}
        if not set(judgments).issubset(known):
            raise HTTPException(status_code=422, detail='Criterion does not belong to the application job')
        grouped = {key: [] for key in WEIGHTS}
        for criterion_ref, row in known.items():
            judgment = judgments.get(criterion_ref)
            status = judgment.status if judgment else 'unknown'
            refs = judgment.evidence_refs if judgment else []
            for ref in refs:
                if not db.execute('SELECT 1 FROM rubric_evidence WHERE id = ? AND application_id = ?',
                                  (int(ref[2:]), application_id)).fetchone():
                    raise HTTPException(status_code=422, detail='Evidence does not belong to the application')
            grouped[row['category']].append({'criterion_ref': criterion_ref, 'status': status,
                'evidence_refs': refs, 'reviewed_by_hr': status != 'unknown',
                'absence_review_attested': judgment.absence_review_attested if judgment else False})
        assessment = Assessment.model_validate(grouped)
        result = calculate_rubric(assessment)
        cursor = db.execute('''INSERT INTO rubric_assessments
            (application_id,actor_id,assessment_json,result_json) VALUES (?,?,?,?)''',
            (application_id, user.id, assessment.model_dump_json(), json.dumps(result)))
        db.execute('INSERT INTO rubric_audit_events (actor_id,action,application_id,reference_id) VALUES (?, ?, ?, ?)',
                   (user.id, 'assessment_created', application_id, cursor.lastrowid))
        return {'id': cursor.lastrowid, 'actor_id': user.id, 'result': result}

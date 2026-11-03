"""Local, provisional keyword screening snapshots for human review."""

import json
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.auth import User, require_roles
from app.db import connect
from app.recruitment import _require_current_role, get_application, score_masked_resume
from app.resume_mask import masked_source
from app.rubric_admin import require_job

router = APIRouter(prefix='/api')
HR = Annotated[User, Depends(require_roles('hr'))]
MAX_SOURCE_CHARS = 4000
MAX_SOURCE_BYTES = MAX_SOURCE_CHARS * 4
MAX_SKILLS = 20
MAX_SKILL_CHARS = 80
MAX_SKILL_JSON_CHARS = 10000


def initialize_provisional_queue(path: str | Path) -> None:
    with connect(path) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS provisional_job_revisions (
            job_id TEXT PRIMARY KEY REFERENCES jobs(id), revision INTEGER NOT NULL DEFAULT 0
        );
        CREATE TRIGGER IF NOT EXISTS provisional_job_changed AFTER UPDATE ON jobs BEGIN
            INSERT OR IGNORE INTO provisional_job_revisions(job_id) VALUES(NEW.id);
            UPDATE provisional_job_revisions SET revision=revision+1 WHERE job_id=NEW.id;
        END;
        CREATE TABLE IF NOT EXISTS provisional_application_revisions (
            application_id INTEGER PRIMARY KEY REFERENCES applications(id), revision INTEGER NOT NULL DEFAULT 0
        );
        CREATE TRIGGER IF NOT EXISTS provisional_application_job_changed
        AFTER UPDATE OF job_id ON applications WHEN OLD.job_id IS NOT NEW.job_id BEGIN
            INSERT OR IGNORE INTO provisional_application_revisions(application_id) VALUES(NEW.id);
            UPDATE provisional_application_revisions SET revision=revision+1 WHERE application_id=NEW.id;
        END;
        CREATE TABLE IF NOT EXISTS provisional_snapshots (
            id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL REFERENCES applications(id),
            job_id TEXT NOT NULL REFERENCES jobs(id), job_revision INTEGER NOT NULL, assignment_revision INTEGER NOT NULL,
            source_context TEXT, source_revision INTEGER, mask_revision INTEGER, source_review_status TEXT NOT NULL,
            automatic_masked INTEGER, additional_masked INTEGER,
            score INTEGER, required_mentioned INTEGER, required_total INTEGER,
            snapshot_kind TEXT NOT NULL CHECK(snapshot_kind IN ('pending','scored')),
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        CREATE INDEX IF NOT EXISTS provisional_snapshot_application ON provisional_snapshots(application_id,id DESC);
        CREATE INDEX IF NOT EXISTS provisional_applications_job_id ON applications(job_id,id);
        CREATE TABLE IF NOT EXISTS provisional_thresholds (
            job_id TEXT PRIMARY KEY REFERENCES jobs(id), threshold INTEGER NOT NULL CHECK(threshold BETWEEN 0 AND 100),
            version INTEGER NOT NULL CHECK(version > 0)
        );
        CREATE TABLE IF NOT EXISTS provisional_threshold_history (
            id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id), threshold INTEGER NOT NULL,
            version INTEGER NOT NULL, actor_id INTEGER NOT NULL REFERENCES users(id),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        ''')


def _job_revision(db, job_id: str) -> int:
    row = db.execute('SELECT revision FROM provisional_job_revisions WHERE job_id=?', (job_id,)).fetchone()
    return row['revision'] if row else 0


def _assignment_revision(db, application_id: int) -> int:
    row = db.execute('SELECT revision FROM provisional_application_revisions WHERE application_id=?', (application_id,)).fetchone()
    return row['revision'] if row else 0


def _source_revisions(db, application_id: int) -> tuple[int, int]:
    row = db.execute('SELECT source_revision,mask_revision FROM resume_mask_state WHERE application_id=?',
                     (application_id,)).fetchone()
    return (row['source_revision'], row['mask_revision']) if row else (0, 0)


def create_pending_snapshot(db, application_id: int, job_id: str, actor_id: int) -> int:
    """Enqueue an upload without reading, redacting, or scoring its resume."""
    row = db.execute('''INSERT INTO provisional_snapshots
        (application_id,job_id,job_revision,assignment_revision,source_review_status,snapshot_kind,created_by)
        VALUES (?,?,?,?,?,?,?)''', (application_id, job_id, _job_revision(db, job_id),
        _assignment_revision(db, application_id), 'unreviewed', 'pending', actor_id))
    return row.lastrowid


def _bounded_job(db, job_id: str) -> dict:
    row = db.execute('SELECT required_skills,preferred_skills FROM jobs WHERE id=?', (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail='Job not found')
    if any(len(row[field]) > MAX_SKILL_JSON_CHARS for field in ('required_skills', 'preferred_skills')):
        raise HTTPException(status_code=422, detail='Job exceeds the local screening work limit; review manually')
    try:
        job = {field: json.loads(row[field]) for field in ('required_skills', 'preferred_skills')}
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail='Job skills are invalid; review manually') from None
    values = list(job.values())
    if (any(not isinstance(value, list) for value in values)
            or sum(len(value) for value in values) > MAX_SKILLS
            or any(not isinstance(skill, str) or len(skill) > MAX_SKILL_CHARS for value in values for skill in value)):
        raise HTTPException(status_code=422, detail='Job exceeds the local screening work limit; review manually')
    return job


def _latest(db, application_id: int):
    return db.execute('SELECT * FROM provisional_snapshots WHERE application_id=? ORDER BY id DESC LIMIT 1',
                      (application_id,)).fetchone()


def _setting(db, job_id: str) -> dict:
    row = db.execute('SELECT threshold,version FROM provisional_thresholds WHERE job_id=?', (job_id,)).fetchone()
    return dict(row) if row else {'threshold': None, 'version': 0}


class ThresholdUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    threshold: StrictInt = Field(ge=0, le=100)
    expected_version: StrictInt = Field(ge=0)


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_snapshot_id: StrictInt | None


@router.get('/jobs/{job_id}/provisional-threshold')
def get_threshold(job_id: str, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        require_job(db, job_id)
        return _setting(db, job_id)


@router.put('/jobs/{job_id}/provisional-threshold')
def put_threshold(job_id: str, payload: ThresholdUpdate, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        require_job(db, job_id)
        current = _setting(db, job_id)
        if current['version'] != payload.expected_version:
            raise HTTPException(status_code=409, detail='Threshold changed; reload before saving')
        version = current['version'] + 1
        db.execute('''INSERT INTO provisional_thresholds(job_id,threshold,version) VALUES(?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET threshold=excluded.threshold,version=excluded.version''',
            (job_id, payload.threshold, version))
        db.execute('INSERT INTO provisional_threshold_history(job_id,threshold,version,actor_id) VALUES(?,?,?,?)',
                   (job_id, payload.threshold, version, user.id))
        _require_current_role(db, request, user, 'hr')
        return {'threshold': payload.threshold, 'version': version}


@router.post('/applications/{application_id}/provisional-screening', status_code=201)
def refresh(application_id: int, payload: RefreshRequest, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        application = db.execute('SELECT id,job_id,status,length(CAST(resume_text AS BLOB)) AS source_bytes FROM applications WHERE id=?',
                                 (application_id,)).fetchone()
        if application is None:
            raise HTTPException(status_code=404, detail='Application not found')
        if application['status'] in ('hired', 'rejected'):
            raise HTTPException(status_code=409, detail='Application is finalized')
        latest = _latest(db, application_id)
        if (latest['id'] if latest else None) != payload.expected_snapshot_id:
            raise HTTPException(status_code=409, detail='Screening changed; reload before refreshing')
        if application['source_bytes'] > MAX_SOURCE_BYTES:
            raise HTTPException(status_code=422, detail='Resume exceeds the local screening work limit; review manually')
        source_text = db.execute('SELECT resume_text FROM applications WHERE id=?', (application_id,)).fetchone()[0]
        if len(source_text) > MAX_SOURCE_CHARS:
            raise HTTPException(status_code=422, detail='Resume exceeds the local screening work limit; review manually')
        job_id = application['job_id']
        job = _bounded_job(db, job_id)
        source = masked_source(db, application_id)
        source_revisions = _source_revisions(db, application_id)
        job_revision = _job_revision(db, job_id)
        assignment_revision = _assignment_revision(db, application_id)

    # Computation happens after the read transaction closes and before the write lock.
    score = score_masked_resume(job, source)
    required = score['required']
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        application = db.execute('SELECT job_id,status FROM applications WHERE id=?', (application_id,)).fetchone()
        latest = _latest(db, application_id)
        if (application is None or application['status'] in ('hired', 'rejected')
                or application['job_id'] != job_id or _job_revision(db, job_id) != job_revision
                or _assignment_revision(db, application_id) != assignment_revision
                or _source_revisions(db, application_id) != source_revisions
                or (latest['id'] if latest else None) != payload.expected_snapshot_id):
            raise HTTPException(status_code=409, detail='Screening inputs changed; reload before refreshing')
        row = db.execute('''INSERT INTO provisional_snapshots
            (application_id,job_id,job_revision,assignment_revision,source_context,source_revision,mask_revision,
             source_review_status,automatic_masked,additional_masked,score,required_mentioned,required_total,
             snapshot_kind,created_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (application_id, job_id, job_revision, assignment_revision, source['context_sha256'],
             *source_revisions, source['status'], int(source['automatic_masked']), int(source['additional_masked']),
             score['score'], sum(item['mentioned'] for item in required), len(required), 'scored', user.id))
        _require_current_role(db, request, user, 'hr')
        return {'snapshot_id': row.lastrowid}


@router.get('/applications/{application_id}/provisional-history')
def history(application_id: int, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        get_application(db, application_id)
        rows = db.execute('''SELECT id,job_id,job_revision,assignment_revision,source_review_status,snapshot_kind,automatic_masked,
            additional_masked,score,required_mentioned,required_total,created_by,created_at
            FROM provisional_snapshots WHERE application_id=? ORDER BY id DESC LIMIT 100''', (application_id,))
        automatic = db.execute('''SELECT j.snapshot_id,j.state,j.job_id,j.job_revision,j.assignment_revision,
            j.source_review_status,j.automatic_masked,j.additional_masked,j.score,j.required_mentioned,
            j.required_total,j.completed_at FROM auto_provisional_jobs j
            JOIN provisional_snapshots s ON s.id=j.snapshot_id WHERE s.application_id=?
            ORDER BY j.snapshot_id DESC LIMIT 100''', (application_id,)).fetchall()
        return {'items': [dict(row) for row in rows],
                'automatic_results': [dict(row) | {'actor': 'system_worker'} for row in automatic]}


def provisional_application_state(db, application_id: int) -> dict:
    """Return current provisional facts without reading resume text or changing state.

    Caller owns the transaction and is responsible for HR authorization.
    """
    application = db.execute('SELECT id,status,job_id FROM applications WHERE id=?', (application_id,)).fetchone()
    if application is None:
        raise HTTPException(status_code=404, detail='Application not found')
    job_id = application['job_id']
    setting = _setting(db, job_id)
    revision = _job_revision(db, job_id)
    latest = _latest(db, application['id'])
    snapshot_id = latest['id'] if latest else None
    auto_state = None
    origin = 'hr' if latest and latest['snapshot_kind'] == 'scored' else None
    if latest and latest['snapshot_kind'] == 'pending':
        automatic = db.execute('SELECT * FROM auto_provisional_jobs WHERE snapshot_id=?', (latest['id'],)).fetchone()
        auto_state = automatic['state'] if automatic else None
        if automatic and auto_state == 'done':
            latest = dict(automatic) | {'id': snapshot_id, 'snapshot_kind': 'scored'}
            origin = 'system_worker'
    current = bool(latest and latest['snapshot_kind'] == 'scored' and latest['job_id'] == job_id
                   and latest['job_revision'] == revision
                   and latest['assignment_revision'] == _assignment_revision(db, application['id'])
                   and (latest['source_revision'], latest['mask_revision']) == _source_revisions(db, application['id']))
    if application['status'] in ('hired', 'rejected'):
        recommendation = 'finalized'
    elif not latest:
        recommendation = 'not_screened'
    elif latest['snapshot_kind'] == 'pending':
        recommendation = 'pending'
    elif not current:
        recommendation = 'stale'
    elif latest['source_review_status'] != 'reviewed':
        recommendation = 'mask_review_required'
    elif latest['required_total'] == 0:
        recommendation = 'no_required_skills'
    elif setting['threshold'] is None:
        recommendation = 'threshold_not_configured'
    elif latest['score'] >= setting['threshold']:
        recommendation = 'priority_review'
    else:
        recommendation = 'needs_review'
    return {'application_id': application['id'], 'application_status': application['status'],
                  'snapshot_id': snapshot_id, 'current': current, 'origin': origin, 'automatic_state': auto_state,
                  'score': latest['score'] if current else None,
                  'source_review_status': latest['source_review_status'] if current else 'unreviewed' if latest and latest['snapshot_kind'] == 'pending' else 'stale',
                  'automatic_masked': bool(latest['automatic_masked']) if current else None,
                  'additional_masked': bool(latest['additional_masked']) if current else None,
                  'recommendation': recommendation, 'threshold': setting['threshold'],
            'threshold_version': setting['version']}


@router.get('/jobs/{job_id}/provisional-queue')
def queue(job_id: str, request: Request, user: HR,
          after: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=50)):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        require_job(db, job_id)
        setting = _setting(db, job_id)
        items = []
        applications = db.execute('SELECT id,status FROM applications WHERE job_id=? AND id>? ORDER BY id LIMIT ?',
                                  (job_id, after, limit + 1)).fetchall()
        has_more = len(applications) > limit
        applications = applications[:limit]
        for application in applications:
            items.append(provisional_application_state(db, application['id']))
        _require_current_role(db, request, user, 'hr')
        heartbeat = db.execute('SELECT MAX(last_heartbeat) FROM auto_provisional_workers').fetchone()[0]
        return {'job_id': job_id, **setting, 'items': items,
                'worker': {'recent_heartbeat': bool(heartbeat is not None and heartbeat >= time.time()-15),
                           'note': 'A recent heartbeat is evidence of a running local worker, not a delivery guarantee.'},
                'next_after': applications[-1]['id'] if has_more else None,
                'notice': 'Pending means no provisional score exists; it is not a negative result. Automatic results are created by a local system worker and do not replace HR review. '
                          'Refresh supports resumes up to 4,000 characters and at most 20 total required and preferred '
                          'skills of up to 80 characters each; review larger inputs manually. '
                          'Local provisional keyword mentions are not verified ability or a hiring decision. '
                          'Automatic redaction may miss identifiers. All applications require human review.'}

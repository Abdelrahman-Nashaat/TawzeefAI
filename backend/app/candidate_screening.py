"""Read-only screening preview for the owner of a candidate application."""

import hashlib
import json
from threading import BoundedSemaphore
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.auth import User, require_roles
from app.db import connect
from app.matching_specialist import matching_snapshot
from app.recruitment import _require_current_role, score_masked_resume
from app.resume_mask import masked_source

router = APIRouter(prefix='/api')
Candidate = Annotated[User, Depends(require_roles('candidate'))]
HR = Annotated[User, Depends(require_roles('hr'))]
MAX_PREVIEW_CHARS = 4000
MAX_PREVIEW_BYTES = MAX_PREVIEW_CHARS * 4
MAX_PREVIEW_SKILLS = 20
MAX_PREVIEW_SKILL_CHARS = 80
MAX_PREVIEW_SKILL_JSON_CHARS = 10000
_preview_workers = BoundedSemaphore(2)


def _preview_revision(db, application_id: int) -> int:
    row = db.execute('SELECT revision FROM rubric_publication_revisions WHERE application_id=?',
                     (application_id,)).fetchone()
    return row['revision'] if row else 0


def _preview_job(db, job_id: str) -> dict:
    row = db.execute('SELECT required_skills,preferred_skills FROM jobs WHERE id=?', (job_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=409, detail='Job requirements unavailable')
    if any(len(row[field]) > MAX_PREVIEW_SKILL_JSON_CHARS for field in ('required_skills', 'preferred_skills')):
        raise HTTPException(status_code=422, detail='Job requirements exceed the preview work limit.')
    try:
        job = {field: json.loads(row[field]) for field in ('required_skills', 'preferred_skills')}
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail='Job requirements cannot be previewed.') from None
    values = list(job.values())
    if (any(not isinstance(value, list) for value in values)
            or sum(len(value) for value in values) > MAX_PREVIEW_SKILLS
            or any(not isinstance(skill, str) or len(skill) > MAX_PREVIEW_SKILL_CHARS
                   for value in values for skill in value)):
        raise HTTPException(status_code=422, detail='Job requirements exceed the preview work limit.')
    return job


class PublicationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    assessment_id: int = Field(gt=0)
    snapshot_digest: str = Field(pattern=r'^[a-f0-9]{64}$')


def _candidate_assessment(snapshot: dict) -> dict:
    """Explicitly select the material reviewed for release to the owner."""
    return {
        'status': 'current',
        'bounds': {key: snapshot['bounds'][key] for key in ('minimum', 'maximum', 'coverage_percent')},
        'components': [
            {**{key: component[key] for key in ('category', 'weight', 'minimum_points',
               'maximum_points', 'covered_weight', 'unconfigured')},
             'counts': {key: component['counts'][key] for key in ('met', 'missing', 'unknown')}}
            for component in snapshot['components']
        ],
        'criteria': [
            {'category': item['category'], 'criterion': item['criterion'], 'status': item['status'],
             'evidence': [entry['excerpt'] for entry in item['evidence']]}
            for item in snapshot['criteria']
        ],
        'notice': 'This is a human-reviewed summary of available evidence, not verified ability or a hiring decision. '
                  'A score range reflects uncertainty; missing evidence does not prove a lack of skill.',
    }


def _release_snapshot(db, application_id: int) -> tuple[str | None, dict, dict | None]:
    try:
        _, snapshot = matching_snapshot(db, application_id)
        if snapshot['status'] != 'current':
            return None, snapshot, None
        preview = _candidate_assessment(snapshot)
        revision_row = db.execute('SELECT revision FROM rubric_publication_revisions WHERE application_id=?',
                                  (application_id,)).fetchone()
        revision = revision_row['revision'] if revision_row else 0
        encoded = json.dumps({'version': 2, 'revision': revision, 'assessment_id': snapshot['assessment_id'],
                              'preview': preview},
                             ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest(), snapshot, preview
    except (ValueError, TypeError, KeyError, OverflowError):
        return None, {'status': 'outdated'}, None


@router.get('/applications/{application_id}/rubric/candidate-publication')
def publication_preview(application_id: int, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        digest, snapshot, preview = _release_snapshot(db, application_id)
        release = db.execute('SELECT assessment_id,snapshot_digest FROM rubric_publications WHERE application_id=?',
                             (application_id,)).fetchone()
        current = snapshot['status'] == 'current'
        published = bool(current and release and release['assessment_id'] == snapshot['assessment_id']
                         and release['snapshot_digest'] == digest)
        _require_current_role(db, request, user, 'hr')
        return {'status': snapshot['status'], 'published': published, 'has_publication': bool(release),
                'assessment_id': snapshot['assessment_id'] if current else None,
                'snapshot_digest': digest if current else None,
                'preview': preview}


@router.put('/applications/{application_id}/rubric/candidate-publication')
def publish_assessment(application_id: int, payload: PublicationRequest, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        digest, snapshot, _ = _release_snapshot(db, application_id)
        if (snapshot['status'] != 'current' or snapshot['assessment_id'] != payload.assessment_id
                or digest != payload.snapshot_digest):
            raise HTTPException(status_code=409, detail='Assessment changed; review the preview again')
        db.execute('''INSERT INTO rubric_publications(application_id,assessment_id,snapshot_digest,published_by)
                   VALUES (?,?,?,?) ON CONFLICT(application_id) DO UPDATE SET
                   assessment_id=excluded.assessment_id,snapshot_digest=excluded.snapshot_digest,
                   published_by=excluded.published_by''', (application_id, payload.assessment_id, digest, user.id))
        db.execute('INSERT INTO rubric_audit_events(actor_id,action,application_id,reference_id) VALUES (?,?,?,?)',
                   (user.id, 'assessment_published', application_id, payload.assessment_id))
        _require_current_role(db, request, user, 'hr')
        return {'published': True}


@router.delete('/applications/{application_id}/rubric/candidate-publication')
def withdraw_assessment(application_id: int, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        if not db.execute('SELECT 1 FROM applications WHERE id=?', (application_id,)).fetchone():
            raise HTTPException(status_code=404, detail='Application not found')
        deleted = db.execute('DELETE FROM rubric_publications WHERE application_id=?', (application_id,)).rowcount
        if deleted:
            db.execute('INSERT INTO rubric_audit_events(actor_id,action,application_id) VALUES (?,?,?)',
                       (user.id, 'assessment_withdrawn', application_id))
        _require_current_role(db, request, user, 'hr')
        return {'published': False}


@router.get('/applications/{application_id}/candidate-assessment')
def candidate_assessment(application_id: int, request: Request, user: Candidate):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'candidate')
        if not db.execute('SELECT 1 FROM applications WHERE id=? AND candidate_id=?',
                          (application_id, user.id)).fetchone():
            raise HTTPException(status_code=404, detail='Application not found')
        release = db.execute('SELECT assessment_id,snapshot_digest FROM rubric_publications WHERE application_id=?',
                             (application_id,)).fetchone()
        result = {'status': 'unavailable'}
        if release:
            digest, snapshot, preview = _release_snapshot(db, application_id)
            if (snapshot['status'] == 'current' and snapshot['assessment_id'] == release['assessment_id']
                    and digest == release['snapshot_digest']):
                result = preview
        _require_current_role(db, request, user, 'candidate')
        return result


@router.get('/applications/{application_id}/candidate-screening')
def candidate_screening(application_id: int, request: Request, user: Candidate):
    """Compute a fresh lexical preview without storing an HR assessment."""
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'candidate')
        application = db.execute('''SELECT job_id,length(CAST(resume_text AS BLOB)) AS source_bytes FROM applications
                                    WHERE id=? AND candidate_id=?''',
                                 (application_id, user.id)).fetchone()
        if application is None:
            raise HTTPException(status_code=404, detail='Application not found')
        if application['source_bytes'] > MAX_PREVIEW_BYTES:
            raise HTTPException(status_code=422, detail='Resume exceeds the preview work limit of 4,000 characters.')
        raw = db.execute('SELECT resume_text FROM applications WHERE id=?', (application_id,)).fetchone()[0]
        if len(raw) > MAX_PREVIEW_CHARS:
            raise HTTPException(status_code=422, detail='Resume exceeds the preview work limit of 4,000 characters.')
        job_id = application['job_id']
        job = _preview_job(db, job_id)
        source = masked_source(db, application_id)
        revision = _preview_revision(db, application_id)

    if not _preview_workers.acquire(blocking=False):
        raise HTTPException(status_code=503, detail='Preview is busy; retry shortly.')
    try:
        result = score_masked_resume(job, source)
    finally:
        _preview_workers.release()
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'candidate')
        current = db.execute('SELECT job_id FROM applications WHERE id=? AND candidate_id=?',
                             (application_id, user.id)).fetchone()
        if current is None or current['job_id'] != job_id or _preview_revision(db, application_id) != revision:
            raise HTTPException(status_code=409, detail='Preview inputs changed; retry with current information.')
        return {'application_id': application_id, 'mode': 'current_preview',
                'score': result['score'], 'required': result['required'],
                'preferred': result['preferred'], 'evidence_status': result['evidence_status'],
                'notice': 'Current keyword-mention preview, not the saved HR screening, '
                          'a verification of experience, or a hiring decision. '
                          'Negation and personal-data redaction are imperfect. '
                          'Preview limits: 4,000 resume characters and 20 job skills of up to 80 characters each.'}

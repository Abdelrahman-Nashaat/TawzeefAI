"""Save explicitly reviewed source passages as an atomic HR evidence bundle.

This endpoint records evidence only; it never changes rubric judgments or
decides whether a person meets a criterion.
"""

import hashlib
import json
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from app.auth import User, require_roles
from app.db import connect
from app.recruitment import _require_current_role, get_application
from app.resume_mask import masked_source
from app.rubric_admin import public_criterion, public_evidence

router = APIRouter(prefix='/api')
HR = Annotated[User, Depends(require_roles('hr'))]


class ReviewedPassage(BaseModel):
    model_config = ConfigDict(extra='forbid')
    criterion_ref: str = Field(pattern=r'^c-[1-9][0-9]{0,3}$')
    start: int = Field(ge=0, strict=True)
    end: int = Field(gt=0, strict=True)
    excerpt: str = Field(min_length=1, max_length=500)
    reviewed: StrictBool

    @model_validator(mode='after')
    def validate_range(self):
        if not self.reviewed or self.end <= self.start or self.end - self.start > 500:
            raise ValueError('Each passage requires explicit review and a valid range')
        return self


class EvidenceBundle(BaseModel):
    model_config = ConfigDict(extra='forbid')
    snapshot: str = Field(pattern=r'^[a-f0-9]{64}$')
    items: list[ReviewedPassage] = Field(min_length=1, max_length=30)

    @model_validator(mode='after')
    def unique_passages(self):
        keys = [(item.criterion_ref, item.start, item.end) for item in self.items]
        if len(set(keys)) != len(keys):
            raise ValueError('Duplicate criterion and passage selections are not allowed')
        return self


def _snapshot(db, application_id: int):
    application = get_application(db, application_id)
    blind = masked_source(db, application_id)
    source = blind['text']
    criteria = [public_criterion(row) for row in db.execute(
        'SELECT * FROM rubric_criteria WHERE job_id=? ORDER BY id', (application['job_id'],))]
    revision = db.execute('SELECT revision FROM rubric_publication_revisions WHERE application_id=?',
                          (application_id,)).fetchone()
    source_hash = hashlib.sha256(source.encode('utf-8')).hexdigest()
    values = {'version': 1, 'application_id': application_id, 'candidate_id': application['candidate_id'],
              'job_id': application['job_id'], 'source_sha256': source_hash, 'source_context': blind['context_sha256'], 'criteria': criteria,
              'revision': revision['revision'] if revision else 0}
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest(), source_hash, source, criteria


@router.get('/applications/{application_id}/rubric/evidence-bundle/context')
def evidence_bundle_context(application_id: int, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        digest, source_hash, _, _ = _snapshot(db, application_id)
        _require_current_role(db, request, user, 'hr')
        return {'snapshot': digest, 'source_sha256': source_hash, 'offset_unit': 'unicode_code_points',
                'notice': 'Review each passage in context; a mention does not establish a qualification.'}


@router.post('/applications/{application_id}/rubric/evidence-bundle')
def save_evidence_bundle(application_id: int, payload: EvidenceBundle, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        digest, _, source, criteria = _snapshot(db, application_id)
        source_context = masked_source(db, application_id)['context_sha256']
        if not secrets.compare_digest(digest, payload.snapshot):
            raise HTTPException(status_code=409, detail='Source or criteria changed; review again')
        valid = {criterion['criterion_ref'] for criterion in criteria}
        for item in payload.items:
            if item.criterion_ref not in valid:
                raise HTTPException(status_code=409, detail='Criterion changed; review again')
            if item.end > len(source) or source[item.start:item.end] != item.excerpt or not item.excerpt.strip():
                raise HTTPException(status_code=409, detail='Passage changed; review again')

        results = []
        for item in payload.items:
            row = db.execute('''SELECT * FROM rubric_evidence
                WHERE application_id=? AND start_offset=? AND end_offset=? AND excerpt=? AND source_context=? ORDER BY id LIMIT 1''',
                (application_id, item.start, item.end, item.excerpt, source_context)).fetchone()
            created = row is None
            if created:
                cursor = db.execute('''INSERT INTO rubric_evidence
                    (application_id,start_offset,end_offset,excerpt,created_by,source_context) VALUES (?,?,?,?,?,?)''',
                    (application_id, item.start, item.end, item.excerpt, user.id, source_context))
                if cursor.lastrowid > 9999:
                    raise HTTPException(status_code=409, detail='Evidence reference limit reached')
                db.execute('INSERT INTO rubric_audit_events(actor_id,action,application_id,reference_id) VALUES (?,?,?,?)',
                           (user.id, 'evidence_created', application_id, cursor.lastrowid))
                row = db.execute('SELECT * FROM rubric_evidence WHERE id=?', (cursor.lastrowid,)).fetchone()
            results.append({'criterion_ref': item.criterion_ref, 'evidence': public_evidence(row), 'created': created})
        _require_current_role(db, request, user, 'hr')
        return {'items': results, 'notice': 'Evidence saved for review; no judgment or score was changed.'}

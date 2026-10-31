"""Human-supervised review queue based on current assessment intervals."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.auth import User, require_roles
from app.db import connect
from app.recruitment import _require_current_role
from app.rubric_admin import require_job
from app.rubric_comparison import compare_in_snapshot

router = APIRouter(prefix='/api')
HR = Annotated[User, Depends(require_roles('hr'))]


def initialize_rubric_triage(path: str | Path) -> None:
    with connect(path) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS rubric_triage_thresholds (
            job_id TEXT PRIMARY KEY REFERENCES jobs(id),
            threshold INTEGER NOT NULL CHECK(threshold BETWEEN 0 AND 100),
            version INTEGER NOT NULL CHECK(version > 0)
        );
        CREATE TABLE IF NOT EXISTS rubric_triage_audit (
            id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
            actor_id INTEGER NOT NULL REFERENCES users(id), threshold INTEGER NOT NULL,
            version INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
        ''')


class ThresholdUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    threshold: StrictInt = Field(ge=0, le=100)
    expected_version: StrictInt = Field(ge=0)


def _setting(db, job_id: str) -> dict:
    row = db.execute('SELECT threshold,version FROM rubric_triage_thresholds WHERE job_id=?', (job_id,)).fetchone()
    return dict(row) if row else {'threshold': None, 'version': 0}


@router.get('/jobs/{job_id}/rubric/triage-threshold')
def get_threshold(job_id: str, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        require_job(db, job_id)
        setting = _setting(db, job_id)
        _require_current_role(db, request, user, 'hr')
        return setting


@router.put('/jobs/{job_id}/rubric/triage-threshold')
def update_threshold(job_id: str, payload: ThresholdUpdate, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        require_job(db, job_id)
        current = _setting(db, job_id)
        if current['version'] != payload.expected_version:
            raise HTTPException(status_code=409, detail='Threshold changed; reload before saving')
        version = current['version'] + 1
        db.execute('''INSERT INTO rubric_triage_thresholds(job_id,threshold,version) VALUES (?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET threshold=excluded.threshold,version=excluded.version''',
                   (job_id, payload.threshold, version))
        db.execute('INSERT INTO rubric_triage_audit(job_id,actor_id,threshold,version) VALUES (?,?,?,?)',
                   (job_id, user.id, payload.threshold, version))
        _require_current_role(db, request, user, 'hr')
        return {'threshold': payload.threshold, 'version': version}


@router.get('/jobs/{job_id}/rubric/triage')
def get_triage(job_id: str, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        require_job(db, job_id)
        setting = _setting(db, job_id)
        comparison = compare_in_snapshot(db, job_id)
        stages = {row['id']: row['status'] for row in db.execute(
            'SELECT id,status FROM applications WHERE job_id=?', (job_id,))}
        items = []
        for item in comparison['applications']:
            bounds = item['bounds']
            if item['status'] != 'current' or bounds is None:
                recommendation = 'unknown'
            elif setting['threshold'] is None:
                recommendation = 'threshold_not_configured'
            elif bounds['minimum'] >= setting['threshold']:
                recommendation = 'priority_review'
            elif bounds['maximum'] < setting['threshold']:
                recommendation = 'needs_review'
            else:
                recommendation = 'inconclusive'
            items.append({'application_id': item['application_id'], 'application_status': stages[item['application_id']],
                          'assessment_id': item['assessment_id'], 'assessment_status': item['status'],
                          'bounds': bounds, 'recommendation': recommendation})
        _require_current_role(db, request, user, 'hr')
        return {'job_id': job_id, **setting, 'items': items,
                'notice': 'Review priority only, based on human judgments; not a hiring decision or verified ability. '
                          'All applications require human review; no applicant is rejected automatically.'}

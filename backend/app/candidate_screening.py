"""Read-only screening preview for the owner of a candidate application."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth import User, require_roles
from app.db import connect
from app.recruitment import _require_current_role, job_data, score_resume

router = APIRouter(prefix='/api')
Candidate = Annotated[User, Depends(require_roles('candidate'))]


@router.get('/applications/{application_id}/candidate-screening')
def candidate_screening(application_id: int, request: Request, user: Candidate):
    """Compute a fresh lexical preview without storing an HR assessment."""
    with connect(request.app.state.database_path) as db:
        # A single write lock binds the source, role and job snapshot until the
        # final authentication check. No network calls or writes occur here.
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'candidate')
        application = db.execute('''SELECT job_id,resume_text FROM applications
                                    WHERE id=? AND candidate_id=?''',
                                 (application_id, user.id)).fetchone()
        if application is None:
            raise HTTPException(status_code=404, detail='Application not found')
        job = db.execute('SELECT * FROM jobs WHERE id=?', (application['job_id'],)).fetchone()
        if job is None:
            raise HTTPException(status_code=409, detail='Job requirements unavailable')
        result = score_resume(job_data(job), application['resume_text'])
        _require_current_role(db, request, user, 'candidate')
        return {'application_id': application_id, 'mode': 'current_preview',
                'score': result['score'], 'required': result['required'],
                'preferred': result['preferred'], 'evidence_status': result['evidence_status'],
                'notice': 'Current keyword-mention preview, not the saved HR screening, '
                          'a verification of experience, or a hiring decision. '
                          'Negation and personal-data redaction are imperfect.'}

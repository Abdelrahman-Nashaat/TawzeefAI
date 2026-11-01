"""Bounded HR view of rubric activity without assessment content or excerpts."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.auth import User, require_roles
from app.db import connect
from app.recruitment import _require_current_role

router = APIRouter(prefix='/api/audit')
HR = Annotated[User, Depends(require_roles('hr'))]


@router.get('/rubric')
def rubric_timeline(request: Request, user: HR,
                    limit: int = Query(default=20, ge=1, le=100),
                    before: int | None = Query(default=None, ge=1),
                    application_id: int | None = Query(default=None, ge=1),
                    job_id: str | None = Query(default=None, min_length=1, max_length=128)):
    """Return a consistent newest-first page from the rubric audit table."""
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        rows = db.execute('''SELECT r.id, r.actor_id, r.action, r.application_id,
                    COALESCE(r.job_id, a.job_id) AS job_id, r.reference_id, r.created_at
                FROM rubric_audit_events r LEFT JOIN applications a ON a.id=r.application_id
                WHERE (? IS NULL OR r.id < ?) AND (? IS NULL OR r.application_id = ?)
                    AND (? IS NULL OR COALESCE(r.job_id, a.job_id) = ?)
                ORDER BY r.id DESC LIMIT ?''',
            (before, before, application_id, application_id, job_id, job_id, limit + 1)).fetchall()
        events = [dict(row) for row in rows[:limit]]
        _require_current_role(db, request, user, 'hr')
    return {'events': events, 'next_before': rows[limit - 1]['id'] if len(rows) > limit else None,
            'notice': 'Rubric audit events only. Résumé excerpts, criterion text and assessment judgments are excluded.'}

"""Bounded, role-restricted recruitment audit timeline.

This view deliberately omits free-text notes and résumé content. It covers
the recruitment audit_events table only, not the separate administration,
rubric, knowledge-document or slot audit tables.
"""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from app.auth import User, require_roles
from app.db import connect
from app.recruitment import _require_current_role

router = APIRouter(prefix='/api/audit')
HR = Annotated[User, Depends(require_roles('hr'))]


def _summary(action: str, details: str) -> dict:
    """Allow only known, non-free-text metadata into the timeline."""
    try:
        data = json.loads(details)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if action == 'decision':
        decision = data.get('decision')
        return {'decision': decision} if decision in ('advance', 'reject', 'hire') else {}
    if action != 'screened':
        return {}
    result = {}
    if data.get('method') == 'keyword_mentions_v1':
        result['method'] = 'keyword_mentions_v1'
    if data.get('evidence_status') in ('limited', 'insufficient'):
        result['evidence_status'] = data['evidence_status']
    for key in ('score', 'required_mentioned', 'required_total', 'preferred_mentioned', 'preferred_total'):
        value = data.get(key)
        if type(value) is int and 0 <= value <= (100 if key == 'score' else 10_000):
            result[key] = value
    return result


@router.get('/timeline')
def audit_timeline(request: Request, user: HR,
                   limit: int = Query(default=20, ge=1, le=100),
                   before: int | None = Query(default=None, ge=1),
                   application_id: int | None = Query(default=None, ge=1),
                   job_id: str | None = Query(default=None, min_length=1, max_length=128),
                   action: str | None = Query(default=None, min_length=1, max_length=100)):
    """Read a consistent, newest-first page with keyset pagination."""
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        rows = db.execute('''SELECT e.id,e.application_id,a.job_id,e.actor_id,e.action,e.details,e.created_at
            FROM audit_events e JOIN applications a ON a.id=e.application_id
            WHERE (? IS NULL OR e.id < ?) AND (? IS NULL OR e.application_id = ?)
              AND (? IS NULL OR a.job_id = ?) AND (? IS NULL OR e.action = ?)
            ORDER BY e.id DESC LIMIT ?''',
            (before, before, application_id, application_id, job_id, job_id, action, action, limit + 1)).fetchall()
        events = [{key: row[key] for key in ('id', 'application_id', 'job_id', 'actor_id', 'action', 'created_at')}
                  | {'summary': _summary(row['action'], row['details'])} for row in rows[:limit]]
        _require_current_role(db, request, user, 'hr')
    return {'events': events, 'next_before': rows[limit - 1]['id'] if len(rows) > limit else None,
            'notice': 'Recruitment audit events only. Free-text notes and résumé excerpts are excluded; '
                      'other audit categories have separate records.'}

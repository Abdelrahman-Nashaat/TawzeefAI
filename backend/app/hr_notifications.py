"""Local, per-HR acknowledgements of application submissions."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth import User, require_roles
from app.db import connect
from app.recruitment import _require_current_role

router = APIRouter(prefix='/api/hr/notifications')
HR = Annotated[User, Depends(require_roles('hr'))]


def initialize_hr_notifications(path: str | Path) -> None:
    with connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS hr_notifications (
            id INTEGER PRIMARY KEY, application_id INTEGER NOT NULL UNIQUE REFERENCES applications(id),
            submitted_job_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE TABLE IF NOT EXISTS hr_notification_reads (
            notification_id INTEGER NOT NULL REFERENCES hr_notifications(id),
            actor_id INTEGER NOT NULL REFERENCES users(id),
            read_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            PRIMARY KEY(notification_id,actor_id)
        );
        """)


@router.get('')
def list_notifications(request: Request, user: HR, before: int | None = Query(default=None, gt=0),
                       limit: int = Query(default=20, ge=1, le=50)):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        rows = db.execute("""SELECT n.id,n.application_id,n.submitted_job_id,n.created_at,
                a.job_id AS current_job_id,a.status AS current_status,
                CASE WHEN r.notification_id IS NULL THEN 0 ELSE 1 END AS is_read
            FROM hr_notifications n JOIN applications a ON a.id=n.application_id
            LEFT JOIN hr_notification_reads r ON r.notification_id=n.id AND r.actor_id=?
            WHERE (? IS NULL OR n.id<?) ORDER BY n.id DESC LIMIT ?""",
            (user.id, before, before, limit + 1)).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
    # A WAL reader retains its original snapshot even if another connection
    # revokes the session. Recheck after releasing that snapshot.
    with connect(request.app.state.database_path) as db:
        _require_current_role(db, request, user, 'hr')
    return {'items': [dict(row) | {'is_read': bool(row['is_read'])} for row in rows],
            'next_before': rows[-1]['id'] if more else None,
            'notice': 'Local submission notifications do not indicate a threshold result or shortlist. Marking read acknowledges an alert only; it does not review or change an application.'}


@router.post('/{notification_id}/read')
def acknowledge(notification_id: int, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN IMMEDIATE')
        _require_current_role(db, request, user, 'hr')
        row = db.execute('SELECT n.id FROM hr_notifications n JOIN applications a ON a.id=n.application_id WHERE n.id=?',
                         (notification_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='Notification not found')
        db.execute('INSERT OR IGNORE INTO hr_notification_reads(notification_id,actor_id) VALUES(?,?)',
                   (notification_id, user.id))
        _require_current_role(db, request, user, 'hr')
        return {'id': notification_id, 'is_read': True}

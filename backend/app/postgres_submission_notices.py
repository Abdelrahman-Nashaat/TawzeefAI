"""HR-only submission inbox for the synthetic PostgreSQL demo."""
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request

from app.auth import User

DDL = '''CREATE TABLE IF NOT EXISTS hr_notification_reads (
    notification_id bigint NOT NULL REFERENCES hr_notifications(id),
    actor_id bigint NOT NULL REFERENCES users(id),
    read_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(notification_id, actor_id))'''


def install_routes(app, connect, authenticate, authorize_locked):
    @app.get('/api/hr/submission-notices')
    def list_notices(request: Request, user: Annotated[User, Depends(authenticate)],
                     before: int | None = Query(default=None, gt=0),
                     limit: int = Query(default=20, ge=1, le=50)):
        with connect() as db:
            authorize_locked(db, request, user, 'hr')
            rows = db.execute('''SELECT n.id,n.application_id,n.submitted_job_id,n.created_at,
                r.notification_id IS NOT NULL AS is_read
                FROM hr_notifications n LEFT JOIN hr_notification_reads r
                ON r.notification_id=n.id AND r.actor_id=%s
                WHERE (%s::bigint IS NULL OR n.id<%s) ORDER BY n.id DESC LIMIT %s''',
                (user.id, before, before, limit + 1)).fetchall()
            authorize_locked(db, request, user, 'hr')
            return {'items': [dict(row) for row in rows[:limit]],
                    'next_before': rows[limit - 1]['id'] if len(rows) > limit else None}

    @app.post('/api/hr/submission-notices/{notice_id}/read')
    def mark_read(notice_id: int, request: Request, user: Annotated[User, Depends(authenticate)]):
        with connect() as db:
            authorize_locked(db, request, user, 'hr')
            if not db.execute('SELECT id FROM hr_notifications WHERE id=%s FOR SHARE', (notice_id,)).fetchone():
                raise HTTPException(status_code=404, detail='Notice not found')
            db.execute('''INSERT INTO hr_notification_reads(notification_id,actor_id)
                VALUES (%s,%s) ON CONFLICT DO NOTHING''', (notice_id, user.id))
            authorize_locked(db, request, user, 'hr')
            return {'id': notice_id, 'is_read': True}

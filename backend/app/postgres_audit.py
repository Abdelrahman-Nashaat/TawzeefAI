"""HR-only, bounded audit metadata for an application in the synthetic demo."""
import json
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request

from app.auth import User


def install_routes(app, connect, authenticate, authorize_locked):
    @app.get('/api/applications/{application_id}/audit')
    def application_audit(application_id: int, request: Request, user: Annotated[User, Depends(authenticate)],
                          before: int | None = Query(default=None, ge=1, le=9223372036854775807),
                          limit: int = Query(default=20, ge=1, le=100)):
        with connect() as db:
            authorize_locked(db, request, user, 'hr')
            if not db.execute('SELECT id FROM applications WHERE id=%s FOR SHARE', (application_id,)).fetchone():
                raise HTTPException(status_code=404, detail='Application not found')
            rows = db.execute('''SELECT id,actor_id,action,details,created_at FROM audit_events
                WHERE application_id=%s AND (%s::bigint IS NULL OR id<%s)
                ORDER BY id DESC LIMIT %s FOR SHARE''',
                (application_id, before, before, limit + 1)).fetchall()
            more = len(rows) > limit
            items = []
            for row in rows[:limit]:
                decision = None
                if row['action'] == 'decision':
                    try:
                        details = json.loads(row['details'])
                        value = details.get('decision') if isinstance(details, dict) else None
                        if value in ('advance', 'hire', 'reject'):
                            decision = value
                    except (TypeError, ValueError):
                        pass
                items.append({'id': row['id'], 'actor_id': row['actor_id'], 'action': row['action'],
                              'decision': decision, 'created_at': row['created_at']})
            authorize_locked(db, request, user, 'hr')
            return {'items': items, 'next_before': items[-1]['id'] if more else None,
                    'notice': 'Audit metadata only; event details and assessment contents are not displayed.'}

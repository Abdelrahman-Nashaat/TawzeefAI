"""HR-only exact extracted-text comparison for the synthetic demo."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.auth import User


def install_routes(app, connect, authenticate, authorize_locked):
    @app.get('/api/applications/{application_id}/duplicates')
    def duplicate_applications(application_id: int, request: Request,
                               user: Annotated[User, Depends(authenticate)]):
        with connect() as db:
            authorize_locked(db, request, user, 'hr')
            source = db.execute('SELECT resume_text FROM applications WHERE id=%s FOR SHARE',
                                (application_id,)).fetchone()
            if source is None:
                raise HTTPException(status_code=404, detail='Application not found')
            matches = []
            # Empty text has no useful identity; do not group blank documents.
            if source['resume_text'].strip():
                matches = db.execute('''SELECT id AS application_id, candidate_id, job_id, status, created_at
                    FROM applications WHERE id<>%s AND resume_text COLLATE "C" = %s COLLATE "C"
                    ORDER BY id DESC LIMIT 51''', (application_id, source['resume_text'])).fetchall()
            authorize_locked(db, request, user, 'hr')
            return {'application_id': application_id, 'match_type': 'exact_extracted_text',
                    'matches': matches[:50], 'truncated': len(matches) > 50}

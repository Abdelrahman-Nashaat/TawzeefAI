"""Explicitly shared, recipient-bound feedback in the local PostgreSQL demo."""
import hashlib
import json
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field, StrictInt

from app.auth import User
from app.postgres_rubric import WEIGHTS, AssessmentItem, _source

DDL = '''CREATE TABLE IF NOT EXISTS candidate_rubric_feedback (
    application_id bigint PRIMARY KEY REFERENCES applications(id),
    recipient_id bigint NOT NULL REFERENCES users(id),
    assessment_event_id bigint NOT NULL REFERENCES hr_rubric_history(audit_event_id),
    source_digest text NOT NULL, explanation text NOT NULL,
    published_by bigint NOT NULL REFERENCES users(id),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp())'''


class Publication(BaseModel):
    audit_event_id: StrictInt = Field(gt=0)
    explanation: str = Field(min_length=1, max_length=2000)


def current_review(db, application_id, event_id, digest):
    row = db.execute('''SELECT h.review_json,h.source_digest,h.reviewer_id,
        e.details,e.action,e.actor_id,e.application_id AS audit_application
        FROM hr_rubric_history h JOIN audit_events e ON e.id=h.audit_event_id
        JOIN hr_rubric_assessments a ON a.application_id=h.application_id
        WHERE h.application_id=%s AND h.audit_event_id=%s
          AND h.audit_event_id=(SELECT max(audit_event_id) FROM hr_rubric_history WHERE application_id=%s)
          AND h.source_digest=a.source_digest AND h.review_json=a.review_json AND h.reviewer_id=a.reviewer_id
        FOR SHARE OF h,e,a''', (application_id, event_id, application_id)).fetchone()
    try:
        details = json.loads(row['details'])
        data = json.loads(row['review_json'])
        items = [AssessmentItem.model_validate(item) for item in data['items']]
        valid = (row['source_digest'] == digest and row['action'] == 'rubric_reviewed'
                 and row['actor_id'] == row['reviewer_id'] and row['audit_application'] == application_id
                 and details.get('source_digest') == digest
                 and details.get('review_digest') == hashlib.sha256(row['review_json'].encode()).hexdigest()
                 and len(items) == 5 and {item.category for item in items} == set(WEIGHTS)
                 and any(item.score is not None for item in items))
        if not valid:
            return None
        lower = sum(WEIGHTS[i.category] * i.score for i in items if i.score is not None) / 100
        upper = lower + sum(WEIGHTS[i.category] for i in items if i.score is None)
        return lower, upper
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def install_routes(app, connect, authenticate, authorize_locked):
    @app.post('/api/applications/{application_id}/candidate-feedback')
    def publish(application_id: int, payload: Publication, request: Request,
                user: Annotated[User, Depends(authenticate)]):
        explanation = payload.explanation.strip()
        if not explanation:
            raise HTTPException(status_code=422, detail='Feedback explanation is required')
        with connect() as db:
            authorize_locked(db, request, user, 'hr')
            row, _, digest = _source(db, application_id)
            owner = db.execute('SELECT candidate_id FROM applications WHERE id=%s', (application_id,)).fetchone()['candidate_id']
            if not db.execute("SELECT 1 FROM users WHERE id=%s AND role='candidate' AND active=1 FOR SHARE", (owner,)).fetchone():
                raise HTTPException(status_code=409, detail='An active candidate is required')
            bounds = current_review(db, application_id, payload.audit_event_id, digest)
            if bounds is None:
                raise HTTPException(status_code=409, detail='A current reviewed assessment is required')
            db.execute('''INSERT INTO candidate_rubric_feedback(application_id,recipient_id,assessment_event_id,source_digest,explanation,published_by)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(application_id) DO UPDATE SET
                recipient_id=EXCLUDED.recipient_id,assessment_event_id=EXCLUDED.assessment_event_id,
                source_digest=EXCLUDED.source_digest,explanation=EXCLUDED.explanation,published_by=EXCLUDED.published_by,
                published_at=clock_timestamp()''', (row['id'],owner,payload.audit_event_id,digest,explanation,user.id))
            db.execute("INSERT INTO audit_events(application_id,actor_id,action,details) VALUES (%s,%s,'feedback_published',%s)",
                       (application_id,user.id,json.dumps({'assessment_event_id': payload.audit_event_id})))
            authorize_locked(db, request, user, 'hr')
            return {'lower': bounds[0], 'upper': bounds[1], 'explanation': explanation, 'assessment_event_id': payload.audit_event_id}

    @app.get('/api/applications/{application_id}/candidate-feedback')
    def read(application_id: int, request: Request, user: Annotated[User, Depends(authenticate)]):
        with connect() as db:
            authorize_locked(db, request, user, 'candidate')
            application = db.execute('SELECT id FROM applications WHERE id=%s AND candidate_id=%s FOR UPDATE',
                                     (application_id, user.id)).fetchone()
            if application is None:
                raise HTTPException(status_code=404, detail='Application not found')
            feedback = db.execute('SELECT * FROM candidate_rubric_feedback WHERE application_id=%s AND recipient_id=%s FOR SHARE',
                                  (application_id,user.id)).fetchone()
            bounds = None
            if feedback:
                try:
                    _, _, digest = _source(db, application_id)
                    if feedback['source_digest'] == digest:
                        bounds = current_review(db, application_id, feedback['assessment_event_id'], digest)
                except HTTPException as error:
                    if error.status_code != 422:
                        raise
            authorize_locked(db, request, user, 'candidate')
            if bounds is None:
                return {'status': 'unavailable'}
            return {'status': 'current', 'lower': bounds[0], 'upper': bounds[1], 'explanation': feedback['explanation'],
                    'notice': 'HR-reviewed assessment; scores do not verify qualifications or determine the hiring decision.'}

    @app.delete('/api/applications/{application_id}/candidate-feedback')
    def retract(application_id: int, request: Request, user: Annotated[User, Depends(authenticate)]):
        with connect() as db:
            authorize_locked(db, request, user, 'hr')
            if not db.execute('SELECT id FROM applications WHERE id=%s FOR UPDATE', (application_id,)).fetchone():
                raise HTTPException(status_code=404, detail='Application not found')
            removed = db.execute('DELETE FROM candidate_rubric_feedback WHERE application_id=%s RETURNING assessment_event_id',
                                 (application_id,)).fetchone()
            if removed:
                db.execute("INSERT INTO audit_events(application_id,actor_id,action,details) VALUES (%s,%s,'feedback_retracted',%s)",
                           (application_id,user.id,json.dumps({'assessment_event_id': removed['assessment_event_id']})))
            authorize_locked(db, request, user, 'hr')
            return {'status': 'retracted' if removed else 'unavailable'}

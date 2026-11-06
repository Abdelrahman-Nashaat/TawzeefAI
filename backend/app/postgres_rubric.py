"""HR-reviewed, evidence-bound rubric for the synthetic PostgreSQL demo."""
import hashlib
import json
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, StrictInt, model_validator

from app.auth import User
from app.blind import redact_resume

WEIGHTS = {'mandatory_skills': 40, 'preferred_skills': 15, 'experience': 20, 'projects': 15, 'education': 10}
DDL = '''CREATE TABLE IF NOT EXISTS hr_rubric_assessments (
    application_id bigint PRIMARY KEY REFERENCES applications(id),
    reviewer_id bigint NOT NULL REFERENCES users(id),
    source_digest text NOT NULL, review_json text NOT NULL,
    reviewed_at timestamptz NOT NULL DEFAULT clock_timestamp())'''

HISTORY_DDL = '''CREATE TABLE IF NOT EXISTS hr_rubric_history (
    audit_event_id bigint PRIMARY KEY REFERENCES audit_events(id),
    application_id bigint NOT NULL REFERENCES applications(id),
    reviewer_id bigint NOT NULL REFERENCES users(id), source_digest text NOT NULL,
    review_json text NOT NULL, reviewed_at timestamptz NOT NULL DEFAULT clock_timestamp())'''
HISTORY_INDEX = 'CREATE INDEX IF NOT EXISTS rubric_history_application ON hr_rubric_history(application_id,audit_event_id DESC)'



class AssessmentItem(BaseModel):
    category: Literal['mandatory_skills', 'preferred_skills', 'experience', 'projects', 'education']
    score: StrictInt | None = Field(ge=0, le=100)
    start: StrictInt | None = Field(default=None, ge=0)
    end: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def valid_evidence(self):
        if self.score is None:
            if self.start is not None or self.end is not None:
                raise ValueError('Unknown categories must not have evidence offsets')
        elif self.start is None or self.end is None or self.end <= self.start:
            raise ValueError('A scored category requires a nonempty evidence span')
        return self


class Assessment(BaseModel):
    source_digest: str = Field(pattern='^[a-f0-9]{64}$')
    items: list[AssessmentItem] = Field(min_length=5, max_length=5)


def _source(db, application_id):
    row = db.execute('''SELECT a.id,a.status,a.resume_text,j.required_skills,j.preferred_skills,
        j.responsibilities,j.title FROM applications a JOIN jobs j ON j.id=a.job_id
        WHERE a.id=%s FOR UPDATE OF a FOR SHARE OF j''', (application_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail='Application not found')
    if len(row['resume_text']) > 12000:
        raise HTTPException(status_code=422, detail='Resume exceeds experimental review limit')
    masked = redact_resume(row['resume_text']).text
    if len(masked) > 16000:
        raise HTTPException(status_code=422, detail='Resume exceeds experimental review limit')
    basis = json.dumps([row['resume_text'], masked, row['title'], row['required_skills'],
                        row['preferred_skills'], row['responsibilities']], ensure_ascii=False, separators=(',', ':'))
    digest = hashlib.sha256(basis.encode('utf-8')).hexdigest()
    return row, masked, digest


def install_routes(app, connect, authenticate, authorize_locked):
    @app.get('/api/applications/{application_id}/rubric-source')
    def source(application_id: int, request: Request, user: Annotated[User, Depends(authenticate)]):
        with connect() as db:
            authorize_locked(db, request, user, 'hr')
            row, masked, digest = _source(db, application_id)
            authorize_locked(db, request, user, 'hr')
            return {'source_digest': digest, 'resume_text': masked, 'job': {
                'title': row['title'], 'required_skills': json.loads(row['required_skills']),
                'preferred_skills': json.loads(row['preferred_skills']),
                'responsibilities': json.loads(row['responsibilities'])},
                'notice': 'Incomplete masking; HR must review the original. Evidence is not verification.'}

    @app.post('/api/applications/{application_id}/rubric-assessment')
    def assess(application_id: int, payload: Assessment, request: Request, user: Annotated[User, Depends(authenticate)]):
        with connect() as db:
            authorize_locked(db, request, user, 'hr')
            row, masked, digest = _source(db, application_id)
            if row['status'] != 'submitted':
                raise HTTPException(status_code=409, detail='Application is not awaiting review')
            if payload.source_digest != digest:
                raise HTTPException(status_code=409, detail='Source changed; review again')
            if {item.category for item in payload.items} != set(WEIGHTS):
                raise HTTPException(status_code=422, detail='Review each category once')
            lower = upper = 0
            for item in payload.items:
                weight = WEIGHTS[item.category]
                if item.score is None:
                    upper += weight * 100
                else:
                    if item.end > len(masked) or not masked[item.start:item.end].strip() or item.end - item.start > 1000:
                        raise HTTPException(status_code=422, detail='Evidence span is invalid')
                    lower += weight * item.score
                    upper += weight * item.score
            review = {'items': [item.model_dump() for item in payload.items],
                      'lower': lower / 100, 'upper': upper / 100}
            data = json.dumps(review, ensure_ascii=False, separators=(',', ':'))
            db.execute('''INSERT INTO hr_rubric_assessments(application_id,reviewer_id,source_digest,review_json)
                VALUES (%s,%s,%s,%s) ON CONFLICT(application_id) DO UPDATE SET reviewer_id=EXCLUDED.reviewer_id,
                source_digest=EXCLUDED.source_digest,review_json=EXCLUDED.review_json,reviewed_at=clock_timestamp()''',
                (application_id,user.id,digest,data))
            event = db.execute("INSERT INTO audit_events(application_id,actor_id,action,details) VALUES (%s,%s,'rubric_reviewed',%s) RETURNING id",
                       (application_id,user.id,json.dumps({'source_digest': digest, 'review_digest': hashlib.sha256(data.encode()).hexdigest()}))).fetchone()
            db.execute("INSERT INTO hr_rubric_history(audit_event_id,application_id,reviewer_id,source_digest,review_json) VALUES (%s,%s,%s,%s,%s)",
                       (event['id'],application_id,user.id,digest,data))
            authorize_locked(db, request, user, 'hr')
            return {'source_digest': digest, **review, 'notice': 'Human assessment is provisional, not an automatic decision.'}

    @app.get('/api/applications/{application_id}/rubric-assessment')
    def read_assessment(application_id: int, request: Request, user: Annotated[User, Depends(authenticate)]):
        with connect() as db:
            authorize_locked(db, request, user, 'hr')
            _, _, digest = _source(db, application_id)
            review = db.execute('SELECT * FROM hr_rubric_assessments WHERE application_id=%s FOR SHARE', (application_id,)).fetchone()
            authorize_locked(db, request, user, 'hr')
            if review is None:
                raise HTTPException(status_code=404, detail='Assessment not found')
            if review['source_digest'] != digest:
                raise HTTPException(status_code=409, detail='Source changed; review again')
            return {'source_digest': digest, 'reviewer_id': review['reviewer_id'],
                    'reviewed_at': review['reviewed_at'], **json.loads(review['review_json'])}

    @app.get('/api/applications/{application_id}/rubric-history')
    def read_history(application_id: int, request: Request, user: Annotated[User, Depends(authenticate)],
                     before: int | None = Query(default=None, gt=0), limit: int = Query(default=20, ge=1, le=50)):
        with connect() as db:
            authorize_locked(db, request, user, 'hr')
            _, _, digest = _source(db, application_id)
            rows = db.execute("""SELECT h.*,e.details,e.actor_id,e.action,e.application_id AS audit_application
                FROM hr_rubric_history h JOIN audit_events e ON e.id=h.audit_event_id
                WHERE h.application_id=%s AND (%s::bigint IS NULL OR h.audit_event_id < %s)
                ORDER BY h.audit_event_id DESC LIMIT %s FOR SHARE OF h,e""",
                (application_id,before,before,limit+1)).fetchall()
            result = []
            for row in rows[:limit]:
                try:
                    details = json.loads(row['details'])
                    review = json.loads(row['review_json'])
                    if (not isinstance(review, dict) or row['action'] != 'rubric_reviewed' or row['actor_id'] != row['reviewer_id'] or
                        row['audit_application'] != application_id or details.get('source_digest') != row['source_digest'] or
                        details.get('review_digest') != hashlib.sha256(row['review_json'].encode()).hexdigest()):
                        raise ValueError('History integrity check failed')
                except (ValueError, TypeError, AttributeError):
                    raise HTTPException(status_code=409, detail='History integrity check failed') from None
                result.append({'audit_event_id': row['audit_event_id'], 'reviewer_id': row['reviewer_id'],
                               'reviewed_at': row['reviewed_at'], 'source_digest': row['source_digest'],
                               'is_stale': row['source_digest'] != digest, **review})
            authorize_locked(db, request, user, 'hr')
            return {'items': result, 'next_before': rows[limit-1]['audit_event_id'] if len(rows) > limit else None,
                    'notice': 'Historical offsets require the original masked source; stale source text is not retained.'}

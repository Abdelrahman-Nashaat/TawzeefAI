"""HR-only comparison of current, human-reviewed rubric assessments.

Ranges describe documented evidence, not verified ability or hiring decisions.
"""

import hashlib
import json
from itertools import combinations
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from app.auth import User, require_roles
from app.blind import redact_resume
from app.db import connect
from app.rubric import (
    Assessment,
    calculate_rubric,
    criteria_fingerprint,
    evidence_is_current,
    result_requires_absence_review,
)
from app.rubric_admin import require_job

router = APIRouter(prefix='/api')
HR = Annotated[User, Depends(require_roles('hr'))]
MAX_APPLICATIONS = 200


@router.get('/jobs/{job_id}/rubric/comparison')
def compare_job_rubrics(job_id: str, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        # A read transaction gives criteria and assessment rows one snapshot.
        db.execute('BEGIN')
        require_job(db, job_id)
        criteria = {f"c-{row['id']}": dict(row) for row in db.execute(
            'SELECT id, category, label FROM rubric_criteria WHERE job_id = ? ORDER BY id', (job_id,))}
        criteria_digest = criteria_fingerprint([{'id': int(ref[2:]), **criterion} for ref, criterion in criteria.items()])
        rows = db.execute('''SELECT a.id AS application_id, a.resume_text, r.id AS assessment_id, r.assessment_json, r.result_json,
                r.criteria_fingerprint, r.source_fingerprint
            FROM applications a LEFT JOIN rubric_assessments r ON r.id = (
                SELECT MAX(x.id) FROM rubric_assessments x WHERE x.application_id = a.id
            ) WHERE a.job_id = ? ORDER BY a.id LIMIT ?''', (job_id, MAX_APPLICATIONS + 1)).fetchall()
        if len(rows) > MAX_APPLICATIONS:
            raise HTTPException(status_code=409, detail='Too many applications for a single comparison')
        evidence_by_application = {}
        for evidence in db.execute('''SELECT e.* FROM rubric_evidence e JOIN applications a
                ON a.id=e.application_id WHERE a.job_id=?''', (job_id,)):
            evidence_by_application.setdefault(evidence['application_id'], []).append(dict(evidence))

    applications = []
    for row in rows:
        item = {'application_id': row['application_id'], 'assessment_id': row['assessment_id'],
                'status': 'not_assessed', 'outdated': False, 'bounds': None, 'questions': []}
        if row['assessment_id'] is not None:
            # Validate the stored judgments and recalculate rather than trusting
            # a score detached from its underlying reviewed criteria.
            try:
                assessment = Assessment.model_validate(json.loads(row['assessment_json']))
                claims = [(category, claim) for category in criteria_categories()
                          for claim in getattr(assessment, category)]
                assessed = {claim.criterion_ref: category for category, claim in claims}
                current = {ref: criterion['category'] for ref, criterion in criteria.items()}
                source = redact_resume(row['resume_text']).text
                outdated = (not criteria or assessed != current or result_requires_absence_review(row['result_json'])
                            or not row['criteria_fingerprint']
                            or row['criteria_fingerprint'] != criteria_digest or not row['source_fingerprint']
                            or row['source_fingerprint'] != hashlib.sha256(
                                source.encode('utf-8')).hexdigest()
                            or not evidence_is_current(assessment, source,
                                                       evidence_by_application.get(row['application_id'], [])))
            except (ValueError, TypeError, ValidationError):
                outdated = True
            if outdated:
                item.update(status='outdated', outdated=True)
            else:
                result = calculate_rubric(assessment)
                item.update(status='current', bounds={
                    'minimum': result['minimum_score'], 'maximum': result['maximum_score'],
                    'coverage_percent': result['coverage_percent'],
                })
                for _, claim in claims:
                    if claim.status in ('unknown', 'missing'):
                        label = criteria[claim.criterion_ref]['label']
                        item['questions'].append({
                            'criterion_ref': claim.criterion_ref, 'criterion': label,
                            'status': claim.status,
                            'question': f'Can you describe any relevant experience or evidence for: {label}?',
                            'question_ar': f'هل يمكنك وصف خبرتك أو تقديم مثال ذي صلة بالمعيار التالي: {label}؟',
                        })
        applications.append(item)

    comparisons = []
    for left, right in combinations((item for item in applications if item['status'] == 'current'), 2):
        l_bound, r_bound = left['bounds'], right['bounds']
        relation = 'inconclusive'
        if l_bound['minimum'] > r_bound['maximum']:
            relation = 'left_higher'
        elif r_bound['minimum'] > l_bound['maximum']:
            relation = 'right_higher'
        comparisons.append({'left_application_id': left['application_id'],
                            'right_application_id': right['application_id'], 'relation': relation})
    return {'job_id': job_id, 'applications': applications, 'comparisons': comparisons,
            'notice': 'These ranges summarize reviewed evidence, not ability or a hiring decision. '
                      'Overlapping or touching ranges are inconclusive; a human must review each assessment.'}


def criteria_categories():
    return ('mandatory', 'preferred', 'experience', 'projects', 'education')

"""Read-only summary of human-reviewed, evidence-linked matching.

The caller is responsible for HR authorization and a consistent database
snapshot. This module does not invoke a model, change assessments, or decide
whether anyone is suitable for a job.
"""

import hashlib
import json

from pydantic import ValidationError

from app.blind import redact_resume
from app.evidence_cautions import evidence_cautions
from app.recruitment import get_application
from app.resume_field_review import _latest as latest_field_review
from app.resume_field_review import _view as field_review_view
from app.resume_fields import MAX_SOURCE_CHARS
from app.rubric import (
    WEIGHTS,
    Assessment,
    calculate_rubric,
    criteria_fingerprint,
    evidence_is_current,
    result_requires_absence_review,
)


def matching_snapshot(db, application_id: int) -> tuple[str, dict]:
    """Return a digest for checkpointing and a report for authorized display."""
    application = get_application(db, application_id)
    source = redact_resume(application['resume_text']).text
    job = db.execute('SELECT * FROM jobs WHERE id=?', (application['job_id'],)).fetchone()
    if job is None:
        raise ValueError('Application job is unavailable')
    criteria = [dict(row) for row in db.execute(
        'SELECT id,category,label FROM rubric_criteria WHERE job_id=? ORDER BY id',
        (application['job_id'],))]
    assessment_row = db.execute('''SELECT id,assessment_json,result_json,created_at,criteria_fingerprint,source_fingerprint FROM rubric_assessments
        WHERE application_id=? ORDER BY id DESC LIMIT 1''', (application_id,)).fetchone()
    assessment_data = dict(assessment_row) if assessment_row else None
    evidence = [dict(row) for row in db.execute('''SELECT id,start_offset,end_offset,excerpt
        FROM rubric_evidence WHERE application_id=? ORDER BY id''', (application_id,))]
    review_row = latest_field_review(db, application_id)
    field_review = dict(review_row) if review_row else None
    raw = json.dumps({'application_id': application_id, 'job': dict(job),
                      'source': source, 'criteria': criteria, 'assessment': assessment_data,
                      'evidence': evidence, 'field_review': field_review}, ensure_ascii=False,
                     sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    if len(source) > MAX_SOURCE_CHARS:
        reviewed_fields = {'status': 'limit_exceeded', 'fields': []}
    else:
        current = field_review_view(review_row, source, hashlib.sha256(source.encode('utf-8')).hexdigest())
        # Do not return the full source. Values are returned only after the
        # caller has independently authorized access to the current snapshot.
        reviewed_fields = {key: current[key] for key in ('status', 'version', 'fields', 'notice')}
    notice = ('Human-reviewed evidence summary only, not a hiring decision or verified ability. '
              'Redaction is incomplete; review the original source and each criterion.')
    if not assessment_data:
        return digest, {'status': 'not_assessed', 'notice': notice, 'reviewed_fields': reviewed_fields}
    known = {f"c-{item['id']}": item for item in criteria}
    try:
        if result_requires_absence_review(assessment_data['result_json']):
            raise ValueError('Assessment absence review is not verifiable')
        if (not assessment_data['criteria_fingerprint'] or
                assessment_data['criteria_fingerprint'] != criteria_fingerprint(criteria)):
            raise ValueError('Assessment criterion definitions cannot be verified')
        if (not assessment_data['source_fingerprint'] or assessment_data['source_fingerprint'] !=
                hashlib.sha256(source.encode('utf-8')).hexdigest()):
            raise ValueError('Assessment source cannot be verified')
        assessment = Assessment.model_validate(json.loads(assessment_data['assessment_json']))
        claims = [(category, claim) for category in WEIGHTS for claim in getattr(assessment, category)]
        if not known or {claim.criterion_ref: category for category, claim in claims} != {
            ref: item['category'] for ref, item in known.items()
        }:
            raise ValueError('Assessment criteria have changed')
        available = {f"e-{item['id']}": item for item in evidence}
        if not evidence_is_current(assessment, source, evidence):
            raise ValueError('Evidence no longer matches its source')
    except (TypeError, ValueError, KeyError, ValidationError):
        return digest, {'status': 'outdated', 'notice': notice + ' A fresh HR assessment is needed.',
                        'reviewed_fields': reviewed_fields}

    result = calculate_rubric(assessment)
    items = []
    for category, claim in claims:
        criterion = known[claim.criterion_ref]
        item = {'criterion_ref': claim.criterion_ref, 'category': category,
                'criterion': criterion['label'], 'status': claim.status,
                'evidence': [{'ref': ref, 'excerpt': available[ref]['excerpt'],
                              'start': available[ref]['start_offset'], 'end': available[ref]['end_offset'],
                              'cautions': evidence_cautions(available[ref]['excerpt'], available[ref]['start_offset'])}
                             for ref in claim.evidence_refs]}
        if claim.status in ('unknown', 'missing'):
            label = criterion['label']
            item['question'] = f'Can you describe relevant experience or evidence for: {label}?'
            item['question_ar'] = f'هل يمكنك وصف خبرتك أو تقديم مثال ذي صلة بالمعيار التالي: {label}؟'
        items.append(item)
    return digest, {'status': 'current', 'assessment_id': assessment_data['id'],
                    'assessed_at': assessment_data['created_at'],
                    'bounds': {'minimum': result['minimum_score'], 'maximum': result['maximum_score'],
                               'coverage_percent': result['coverage_percent']},
                    'criteria': items, 'notice': notice, 'reviewed_fields': reviewed_fields}

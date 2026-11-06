"""Build an HR-only, evidence-linked interview guide from reviewed criteria.

Questions are deterministic prompts for human review. They do not evaluate an
applicant, verify experience, or create hiring decisions.
"""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth import User, require_roles
from app.blind import redact_resume
from app.db import connect
from app.matching_specialist import matching_snapshot
from app.recruitment import _require_current_role

router = APIRouter(prefix='/api')
HR = Annotated[User, Depends(require_roles('hr'))]

# This filter is deliberately narrow and incomplete; it catches common direct
# protected-attribute and contact references but cannot identify all proxies.
_UNSUITABLE = re.compile(
    r'(?<!\w)(?:name|first\s+name|last\s+name|age|date\s+of\s+birth|birth(?:day|date)|gender|sex|race|ethnicity|'
    r'nationality|religion|religious|marital\s+status|pregnan\w*|disabilit\w*|'
    r'address|phone|e-?mail|citizenship|national\s+origin|'
    r'\u0627\u0644\u0627\u0633\u0645|\u0627\u0644\u0639\u0645\u0631|\u0627\u0644\u0633\u0646|'
    r'\u0627\u0644\u062c\u0646\u0633|\u0627\u0644\u062c\u0646\u0633\u064a\u0629|'
    r'\u0627\u0644\u062f\u064a\u0646|\u0627\u0644\u062d\u0627\u0644\u0629\s+\u0627\u0644\u0627\u062c\u062a\u0645\u0627\u0639\u064a\u0629|'
    r'\u062a\u0627\u0631\u064a\u062e\s+\u0627\u0644\u0645\u064a\u0644\u0627\u062f)(?!\w)',
    re.IGNORECASE,
)

_PROMPTS = {
    'mandatory': 'What example, if any, can you share of using {criterion}? Please describe your contribution and outcome.',
    'preferred': 'What experience, if any, do you have with {criterion}? Please give an example.',
    'experience': 'What relevant work or other experience, if any, can you describe for {criterion}? What was your role?',
    'projects': 'What project, if any, demonstrates {criterion}? What did you personally contribute?',
    'education': 'What relevant learning, training, or coursework, if any, relates to {criterion}?',
}


def _safe_criterion(label: object) -> bool:
    if not isinstance(label, str) or not label.strip() or len(label) > 200:
        return False
    if any(character in label for character in '\r\n') or _UNSUITABLE.search(label):
        return False
    # Reuse the existing bounded contact/identity redactor to avoid inserting
    # obvious contact values or labelled identity fields into a question.
    return redact_resume(label).text == label


def guide_from_matching(snapshot: dict) -> dict:
    """Create questions without copying candidate excerpts or deciding ability."""
    notice = ('Suggested questions require HR review for job relevance and fairness. '
              'Evidence references are not verified qualifications; missing cues do not establish support. '
              'The direct-attribute filter is incomplete and cannot identify every protected trait or proxy.')
    if snapshot.get('status') != 'current':
        return {'status': snapshot.get('status', 'unavailable'), 'questions': [], 'omitted_count': 0,
                'notice': notice + ' A current human-reviewed assessment is required.'}
    questions = []
    omitted = 0
    for item in snapshot.get('criteria', []):
        label, category = item.get('criterion'), item.get('category')
        if not _safe_criterion(label) or category not in _PROMPTS:
            omitted += 1
            continue
        references = [entry['ref'] for entry in item.get('evidence', []) if isinstance(entry.get('ref'), str)]
        cues = sorted({cue['kind'] for entry in item.get('evidence', [])
                       for cue in entry.get('cautions', []) if cue.get('kind') in ('negation', 'aspiration', 'other_person')})
        questions.append({'criterion_ref': item['criterion_ref'], 'category': category,
                          'criterion': label, 'assessment_status': item['status'],
                          'question': _PROMPTS[category].format(criterion=label.strip()),
                          'evidence_refs': references, 'caution_types': cues,
                          'follow_up': ('The reviewed excerpt contains a possible text cue. Ask for clarification '
                                        'of the applicant’s own role without assuming a contradiction.' if cues else None)})
    return {'status': 'current', 'assessment_id': snapshot['assessment_id'], 'questions': questions,
            'omitted_count': omitted, 'notice': notice}


@router.get('/applications/{application_id}/interview-guide')
def interview_guide(application_id: int, request: Request, user: HR):
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        digest, snapshot = matching_snapshot(db, application_id)
        result = guide_from_matching(snapshot)
    # Reopen the read snapshot: rechecking inside the original SQLite read
    # transaction would continue to see a revoked session or stale evidence.
    with connect(request.app.state.database_path) as db:
        db.execute('BEGIN')
        _require_current_role(db, request, user, 'hr')
        current_digest, _ = matching_snapshot(db, application_id)
        if current_digest != digest:
            raise HTTPException(status_code=409, detail='Assessment changed; retry request')
        _require_current_role(db, request, user, 'hr')
        return result

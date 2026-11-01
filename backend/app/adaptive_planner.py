"""Bounded read-only planning for an HR review; model output is untrusted."""

import hashlib
import http.client
import json
import os
import urllib.request

from app.agent import _NoRedirect
from app.ai_provider import GROQ_MODEL, GROQ_URL
from app.recruitment import get_application

ALLOWED = frozenset({'parser', 'matching', 'semantic', 'status', 'scheduler'})
MAX_STEPS = 5


def context_for(db, application_id: int) -> tuple[dict, str]:
    """Return minimal planner facts and a digest of all review inputs."""
    application = get_application(db, application_id)
    job = db.execute('SELECT * FROM jobs WHERE id=?', (application['job_id'],)).fetchone()
    if job is None:
        raise ValueError('Application job is unavailable')
    facts = {'status': application['status'], 'has_screening': bool(application['score_json'])}
    protected = {'application': dict(application), 'job': dict(job)}
    raw = json.dumps(protected, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return facts, hashlib.sha256(raw).hexdigest()


def default_steps(facts: dict) -> list[str]:
    if facts.get('goal') == 'advance_and_reserve':
        return ['parser', 'matching', 'semantic', 'status'] if facts.get('semantic_available') else ['parser', 'matching', 'status']
    if facts['status'] in ('hired', 'rejected'):
        return ['status']
    if not facts['has_screening']:
        return ['parser', 'matching', 'semantic', 'status'] if facts.get('semantic_available') else ['parser', 'matching', 'status']
    if facts['status'] == 'advanced':
        return ['status', 'scheduler']
    return ['status']


def validate_steps(proposal: object, facts: dict) -> list[str]:
    """Reject arguments, identity changes, writes, omissions and bad order."""
    if not isinstance(proposal, dict) or set(proposal) != {'steps'}:
        raise ValueError('Invalid plan format')
    steps = proposal['steps']
    if (not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS or
            any(not isinstance(step, str) or step not in ALLOWED for step in steps) or
            len(set(steps)) != len(steps)):
        raise ValueError('Invalid plan steps')
    if 'status' not in steps:
        raise ValueError('Status is required')
    if facts.get('goal') == 'advance_and_reserve' and (
            facts['status'] != 'submitted' or 'parser' not in steps or 'matching' not in steps):
        raise ValueError('Advancement requires a submitted application, parser and matching')
    if facts['status'] in ('hired', 'rejected') and steps != ['status']:
        raise ValueError('Terminal applications permit only status')
    if 'scheduler' in steps and (facts['status'] != 'advanced' or steps.index('scheduler') < steps.index('status')):
        raise ValueError('Scheduling requires an advanced application and status first')
    if 'parser' in steps and 'matching' in steps and steps.index('parser') > steps.index('matching'):
        raise ValueError('Parsing must precede matching')
    if 'semantic' in steps and (not facts.get('semantic_available') or 'parser' not in steps or
                                'matching' not in steps or not steps.index('parser') < steps.index('matching') < steps.index('semantic')):
        raise ValueError('Semantic review requires enabled screening and prior parsing and matching')
    if (facts['status'] not in ('hired', 'rejected') and not facts['has_screening'] and
            ('parser' not in steps or 'matching' not in steps)):
        raise ValueError('Unscreened applications require parsing and matching')
    return list(steps)


def _request_model(facts: dict, key: str) -> object:
    # Deliberately no identity, CV text, job description, user prompt or tools.
    body = json.dumps({'model': GROQ_MODEL, 'temperature': 0, 'max_completion_tokens': 512,
        'reasoning_effort': 'low',
        'response_format': {'type': 'json_schema', 'json_schema': {
            'name': 'recruitment_review_plan', 'strict': True,
            'schema': {'type': 'object', 'properties': {
                'steps': {'type': 'array', 'items': {'type': 'string',
                    'enum': ['parser', 'matching', 'semantic', 'status', 'scheduler']}}},
                'required': ['steps'], 'additionalProperties': False}}},
        'messages': [
            {'role': 'system', 'content': 'Return JSON only: {"steps":[...]} using only parser, matching, semantic, status, scheduler. '
             'Use status always; never use scheduler unless advanced and after status. '
             'Use semantic only when semantic_available is true and after parser then matching. '
             'For unscreened nonterminal applications use parser then matching. '
             'For hired or rejected use only status. No additional keys or arguments.'},
            {'role': 'user', 'content': json.dumps(facts)},
        ]}).encode('utf-8')
    request = urllib.request.Request(GROQ_URL, data=body, method='POST', headers={
        'Authorization': f'Bearer {key}', 'Content-Type': 'application/json',
        'User-Agent': 'TawzeefAI/0.1'})
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=20) as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError('Provider response too large')
    response_data = json.loads(raw)
    content = response_data['choices'][0]['message']['content']
    if not isinstance(content, str) or len(content) > 4096:
        raise ValueError('Invalid provider response')
    return json.loads(content)


def make_plan(facts: dict, *, enabled: bool) -> tuple[list[str], str]:
    key = os.environ.get('GROQ_API_KEY', '') if enabled else ''
    if key:
        try:
            return validate_steps(_request_model(facts, key), facts), 'model'
        except (ValueError, TypeError, KeyError, IndexError, OSError, http.client.HTTPException):
            # Never return provider data or exception details to the caller.
            return validate_steps({'steps': default_steps(facts)}, facts), 'deterministic_fallback'
    return validate_steps({'steps': default_steps(facts)}, facts), 'deterministic'

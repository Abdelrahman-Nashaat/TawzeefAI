"""A bounded, read-only model-assisted recruitment review suggestion."""
import http.client
import json
import os
import urllib.request

from app.ai_provider import GROQ_MODEL, GROQ_URL, MAX_RESPONSE_BYTES, ProviderUnavailable, _open


def allowed_actions(status, matching, interview, available):
    """Return review-only options from server-derived enum values."""
    if status == 'submitted':
        return ['review_rubric', 'review_advance'] if matching == 'current' else ['review_rubric']
    if status == 'advanced':
        if interview == 'scheduled':
            return ['review_scheduled_interview']
        if interview == 'completed':
            return ['review_offer_or_next_interview', 'review_interview_schedule']
        return ['candidate_can_book', 'review_interview_schedule'] if available else ['review_interview_schedule']
    if status == 'offered':
        return ['review_offer_status']
    if status in ('hired', 'rejected'):
        return ['review_closed_application']
    return ['review_application_status']


def request_plan(facts, actions):
    """Send only bounded status facts; reject anything except one allowed action."""
    key = os.environ.get('GROQ_API_KEY', '').strip()
    if not key:
        raise ProviderUnavailable('The configured model is unavailable.')
    body = json.dumps({'model': GROQ_MODEL, 'temperature': 0, 'max_completion_tokens': 100,
        'response_format': {'type': 'json_object'},
        'messages': [
            {'role': 'system', 'content': 'Select one review action from the allowed list. Return JSON with exactly one string key named action. Never decide or execute hiring, rejection, booking, or messaging.'},
            {'role': 'user', 'content': json.dumps({'facts': facts, 'allowed_actions': actions})},
        ]}).encode()
    request = urllib.request.Request(GROQ_URL, data=body, method='POST', headers={
        'Authorization': f'Bearer {key}', 'Content-Type': 'application/json', 'User-Agent': 'TawzeefAI/0.1'})
    try:
        with _open(request) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError('Response too large')
        message = json.loads(raw)['choices'][0]['message']['content']
        value = json.loads(message)
        if not isinstance(value, dict) or set(value) != {'action'} or value['action'] not in actions:
            raise ValueError('Invalid action')
        return value['action']
    except (OSError, ValueError, TypeError, KeyError, IndexError, http.client.HTTPException):
        raise ProviderUnavailable('The configured model is unavailable.') from None

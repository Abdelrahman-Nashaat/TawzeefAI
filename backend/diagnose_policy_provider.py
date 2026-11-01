"""Inspect provider availability using fictional policy data and sanitized events.

This diagnostic sends synthetic content to the configured provider. It never
prints keys, requests, responses, exception messages, or response headers.
"""
import argparse
import json
import os
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from app import policy_agent
from app.knowledge import Source


def diagnose(iterations=10, varied=False):
    key = os.environ.get('GROQ_API_KEY', '').strip()
    if not key:
        raise RuntimeError('A provider key is required in the process environment')
    if not 1 <= iterations <= 50:
        raise ValueError('Iterations must be between 1 and 50')
    original = urllib.request.OpenerDirector.open
    events = []
    current_stage = ''

    def observed_open(self, *args, **kwargs):
        try:
            response = original(self, *args, **kwargs)
            events.append({'stage': current_stage, 'outcome': f'http_{response.status}'})
            return response
        except urllib.error.HTTPError as error:
            events.append({'stage': current_stage, 'outcome': f'http_{error.code}'})
            raise
        except Exception as error:
            events.append({'stage': current_stage, 'outcome': type(error).__name__})
            raise

    urllib.request.OpenerDirector.open = observed_open
    outcomes = []
    source = Source(id='synthetic-policy', title='Remote work',
                    text='Staff may work remotely two days per week with manager approval.')
    question = 'Can employees work from home?'
    if varied:
        directory = Path(__file__).resolve().parents[1] / 'data' / 'capstone_samples'
        cases = json.loads((directory / 'policy-qa.json').read_text(encoding='utf-8'))['cases']
        sections = json.loads((directory / 'handbook.json').read_text(encoding='utf-8'))['sections']
        lookup = {item['id']: item for item in sections}
        probes = []
        for case in cases:
            section = lookup[case['expected_section_ids'][0]]
            probes.append((case['id'], case['question'], Source(
                id='synthetic-policy', title=section['title'],
                text=f"{section['text_en']}\n{section['text_ar']}")))
    else:
        probes = [(f'iteration-{index}', question, source) for index in range(iterations)]
    try:
        for case_id, question, source in probes:
            for stage in ('search', 'quote'):
                current_stage = stage
                try:
                    if stage == 'search':
                        message = policy_agent._request_search(question, key)
                        status = 'valid_search' if policy_agent._search_query(message, question) else 'declined_or_invalid'
                    else:
                        message = policy_agent._request_quote_selection(question, [
                            {'id': source.id, 'title': source.title, 'text': source.text}])
                        status = 'validated_quote' if policy_agent._grounded_quotes(message, [source]) else 'abstain'
                except Exception as error:  # noqa: BLE001 - diagnostic records only exception class.
                    status = type(error).__name__
                outcomes.append({'case_id': case_id, 'stage': stage, 'outcome': status})
    finally:
        urllib.request.OpenerDirector.open = original
    return {'cases': len(probes), 'http_events': events, 'model_outcomes': outcomes,
            'http_counts': dict(Counter(f"{item['stage']}:{item['outcome']}" for item in events)),
            'model_outcome_counts': dict(Counter(f"{item['stage']}:{item['outcome']}" for item in outcomes)),
            'notice': ('Bundled fictional questions and passages; not an accuracy or provider reliability benchmark.'
                       if varied else 'One fictional question and passage; not an accuracy or provider reliability benchmark.')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--varied', action='store_true', help='Use the bundled synthetic question set')
    args = parser.parse_args()
    print(json.dumps(diagnose(varied=args.varied), indent=2))

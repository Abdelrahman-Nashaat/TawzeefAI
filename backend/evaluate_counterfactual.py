"""Reproduce a small synthetic identity counterfactual through local HTTP APIs.

This is a diagnostic of chosen examples, not a fairness certification or ranking study.
It uses fictional data, a disposable database, and no external AI provider.
"""

import hashlib
import json
import os
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from docx import Document
from fastapi.testclient import TestClient

from app.main import create_app

FIXTURE = Path(__file__).resolve().parent.parent / 'data' / 'fixtures' / 'counterfactual-screening-v1.json'


def load_cases(path=FIXTURE):
    raw = Path(path).read_bytes()
    data = json.loads(raw.decode('utf-8-sig'))
    if data.get('version') != 'synthetic-counterfactual-v1' or not isinstance(data.get('cases'), list):
        raise ValueError('Invalid counterfactual fixture version or cases')
    ids = set()
    for case in data['cases']:
        identities = case.get('identity_lines')
        if (not isinstance(case.get('id'), str) or not case['id'] or case['id'] in ids
                or not isinstance(identities, list) or len(identities) != 2
                or any(not isinstance(value, str) or not value.strip() or '\n' in value or '\r' in value for value in identities)
                or identities[0] == identities[1] or not isinstance(case.get('body'), str) or not case['body'].strip()
                or not isinstance(case.get('skill'), str) or not case['skill'].strip()
                or case.get('language') not in ('en', 'ar')):
            raise ValueError('Invalid counterfactual pair')
        ids.add(case['id'])
    if not ids:
        raise ValueError('Counterfactual fixture must not be empty')
    return data, hashlib.sha256(raw).hexdigest()


def _docx(identity, body):
    document = Document()
    document.add_paragraph(identity)
    for line in body.splitlines():
        document.add_paragraph(line)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def _expect(response, expected):
    if response.status_code != expected:
        raise RuntimeError(f'Local diagnostic endpoint returned {response.status_code}, expected {expected}')
    return response.json()


def _login(client, role):
    _expect(client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'}), 200)


def _screen(client, application_id):
    result = _expect(client.post(f'/api/applications/{application_id}/screen'), 200)
    return {'score': result['score'], 'required_mentions': [
        {'skill': item['skill'], 'mentioned': item['mentioned']} for item in result['required']
    ]}


def evaluate(fixture=FIXTURE):
    data, source_hash = load_cases(fixture)
    # Ensure inherited developer settings cannot send content to a provider or run external dispatch.
    disabled = {'TAWZEEF_ENABLE_GROQ': '0', 'TAWZEEF_ENABLE_GOOGLE': '0', 'TAWZEEF_AUTO_PROVISIONAL': '0'}
    cases = []
    with patch.dict(os.environ, disabled), TemporaryDirectory(prefix='tawzeef-counterfactual-') as directory:  # noqa: SIM117
        with TestClient(create_app(Path(directory) / 'diagnostic.sqlite3', seed_demo=True)) as client:
            for index, case in enumerate(data['cases']):
                job_id = f'counterfactual-{index}'
                _login(client, 'hr')
                _expect(client.post('/api/jobs', json={'id': job_id, 'title': 'Synthetic diagnostic role',
                    'location': 'Fictional', 'required_skills': [case['skill']], 'preferred_skills': [],
                    'responsibilities': ['Review fictional evidence']}), 201)
                variants = []
                for identity in case['identity_lines']:
                    _login(client, 'candidate')
                    application = _expect(client.post('/api/applications', params={'job_id': job_id, 'filename': 'synthetic.docx'},
                                                      content=_docx(identity, case['body'])), 201)
                    application_id = application['id']
                    # Candidate-owned applications do not grant access to HR screening or masking.
                    denied_screen = client.post(f'/api/applications/{application_id}/screen').status_code
                    denied_mask = client.get(f'/api/applications/{application_id}/resume/mask').status_code
                    if (denied_screen, denied_mask) != (403, 403):
                        raise RuntimeError('Candidate access to HR diagnostic endpoints was not denied')
                    _login(client, 'hr')
                    before = _screen(client, application_id)
                    mask = _expect(client.get(f'/api/applications/{application_id}/resume/mask'), 200)
                    # The fixture has an unlabelled, unique identity line. Never guess another span.
                    start = mask['source'].find(identity)
                    if start < 0 or mask['source'].find(identity, start + 1) != -1:
                        raise RuntimeError('Identity span unavailable or ambiguous; no comparison was made')
                    _expect(client.put(f'/api/applications/{application_id}/resume/mask', json={
                        'source_sha256': mask['source_sha256'], 'expected_source_revision': mask['source_revision'],
                        'expected_revision': mask['revision'], 'spans': [{'start': start, 'end': start + len(identity)}],
                    }), 200)
                    after = _screen(client, application_id)
                    variants.append({'candidate_hr_endpoints': {'screen_status': denied_screen, 'mask_status': denied_mask},
                                     'before': before, 'after_reviewed_mask': after})
                cases.append({'id': case['id'], 'language': case['language'], 'variants': variants,
                    'before_equal': variants[0]['before'] == variants[1]['before'],
                    'after_reviewed_mask_equal': variants[0]['after_reviewed_mask'] == variants[1]['after_reviewed_mask']})
    return {'fixture_version': data['version'], 'fixture_sha256': source_hash, 'case_count': len(cases),
            'before_equal_count': sum(case['before_equal'] for case in cases),
            'after_reviewed_mask_equal_count': sum(case['after_reviewed_mask_equal'] for case in cases),
            'cases': cases,
            'notice': 'Synthetic development diagnostic; the variants differ only in a fictional identity line. '
                      'The program selects known spans to simulate HR masking, not an independent human review. '
                      'Equality compares scores and required-skill mention flags, not evidence excerpts or ranking. '
                      'Equal outputs do not establish fairness or anonymity; differences identify a failure in these examples, not population-level bias.'}


if __name__ == '__main__':
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))

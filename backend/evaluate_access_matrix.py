"""Exercise a small synthetic role-and-ownership matrix through local API endpoints.

The result is a diagnostic of these cases, not proof of zero leaks or production security.
"""
import json
import os
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from docx import Document
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.db import connect
from app.main import create_app

CV_MARKER = 'syntheticresumeunique713'
POLICY_MARKER = 'syntheticpolicyunique642'
CHAT_MARKER = 'syntheticconversationsentinel927'


def evaluate_response(response, expected_status, marker=None, marker_expected=False):
    """Classify an individual observation without treating errors as successes."""
    if response.status_code >= 500:
        return {'status': 'error', 'http_status': response.status_code, 'marker_present': None}
    present = marker in response.text if marker else None
    passed = response.status_code == expected_status and (marker is None or present == marker_expected)
    return {'status': 'pass' if passed else 'fail', 'http_status': response.status_code, 'marker_present': present}


def _must(response, expected):
    if response.status_code != expected:
        raise RuntimeError(f'Synthetic setup failed: HTTP {response.status_code}, expected {expected}')
    return response.json()


def _document():
    document = Document()
    document.add_paragraph(f'Python SQL {CV_MARKER}')
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def evaluate():
    env = {'TAWZEEF_ENABLE_GROQ': '0', 'TAWZEEF_ENABLE_GOOGLE': '0',
           'TAWZEEF_AUTO_PROVISIONAL': '0', 'TAWZEEF_ENABLE_HYBRID': '0',
           'TAWZEEF_USE_POLICY_HYBRID': '0', 'TAWZEEF_USE_HYBRID_ANSWERS': '0'}
    observations = []
    with patch.dict(os.environ, env), TemporaryDirectory(prefix='tawzeef-access-diagnostic-') as directory:
        database = Path(directory) / 'diagnostic.sqlite3'
        with TestClient(create_app(database, seed_demo=True)) as client:
            with connect(database) as db:
                db.execute("INSERT INTO users(username,display_name,role,password_hash) VALUES ('candidate_b','Fictional B','candidate',?)",
                           (hash_password('candidate-b-demo-only'),))

            sessions = {}

            def login(role):
                client.cookies.clear()
                if role not in sessions:
                    password = 'candidate-b-demo-only' if role == 'candidate_b' else f'{role}-demo-only'
                    _must(client.post('/api/auth/login', json={'username': role, 'password': password}), 200)
                    sessions[role] = dict(client.cookies)
                else:
                    client.cookies.update(sessions[role])

            def check(case, role, method, path, expected_status, marker=None, marker_expected=False, **kwargs):
                try:
                    if role == 'anonymous':
                        client.cookies.clear()
                    else:
                        login(role)
                    response = client.request(method, path, **kwargs)
                    observed = evaluate_response(response, expected_status, marker, marker_expected)
                except Exception as exc:  # noqa: BLE001 - errors are explicit outcomes, not successful denials
                    observed = {'status': 'error', 'http_status': None, 'marker_present': None,
                                'error_type': type(exc).__name__}
                observations.append({'id': case, 'role': role, 'method': method, 'endpoint': path,
                                     'expected_http_status': expected_status,
                                     'marker_expected': marker_expected if marker else None, **observed})

            login('candidate')
            own = _must(client.post('/api/applications', params={'job_id': 'job-backend-python', 'filename': 'synthetic.docx'},
                                    content=_document()), 201)['id']
            chat = _must(client.post('/api/conversations', json={'title': CHAT_MARKER}), 201)['id']
            login('candidate_b')
            other = _must(client.post('/api/applications', params={'job_id': 'job-data-engineer', 'filename': 'synthetic.docx'},
                                      content=_document()), 201)['id']
            login('hr')
            _must(client.post(f'/api/applications/{own}/screen'), 200)
            _must(client.post('/api/knowledge/documents/text', json={'title': 'Quasar leave policy',
                'text': f'Quasar leave policy marker {POLICY_MARKER} governs fictional internal procedures.',
                'collection': 'handbook', 'audience': 'hr'}), 201)

            for role, expected in (('anonymous', 401), ('candidate', 403), ('candidate_b', 403), ('employee', 403), ('hr', 200)):
                check(f'dashboard-{role}', role, 'GET', '/api/dashboard', expected)
            check('candidate-own-application', 'candidate', 'GET', f'/api/applications/{own}', 200, CV_MARKER, False)
            check('candidate-other-application', 'candidate', 'GET', f'/api/applications/{other}', 403, CV_MARKER, False)
            check('other-candidate-own-application', 'candidate_b', 'GET', f'/api/applications/{other}', 200, CV_MARKER, False)
            check('employee-application', 'employee', 'GET', f'/api/applications/{own}', 403, CV_MARKER, False)
            check('hr-application-positive-control', 'hr', 'GET', f'/api/applications/{own}', 200, CV_MARKER, True)
            for role, status in (('candidate', 403), ('candidate_b', 403), ('employee', 403), ('hr', 200)):
                check(f'mask-{role}', role, 'GET', f'/api/applications/{own}/resume/mask', status,
                      CV_MARKER, role == 'hr')
            check('candidate-screen-write-denied', 'candidate', 'POST', f'/api/applications/{own}/screen', 403, CV_MARKER)
            check('candidate-screen-history-denied', 'candidate', 'GET', f'/api/applications/{own}/screening-history', 403, CV_MARKER)
            check('hr-screen-history-positive-control', 'hr', 'GET', f'/api/applications/{own}/screening-history', 200)
            check('candidate-other-chat-denied', 'candidate_b', 'GET', f'/api/conversations/{chat}', 404, CHAT_MARKER)
            check('candidate-own-chat-positive-control', 'candidate', 'GET', f'/api/conversations/{chat}', 200, CHAT_MARKER, True)
            for role in ('candidate', 'employee', 'hr'):
                login(role)
                current_chat = _must(client.post('/api/conversations', json={'title': 'Fictional policy diagnostic'}), 201)['id']
                check(f'policy-{role}', role, 'POST', f'/api/conversations/{current_chat}/messages', 201,
                      POLICY_MARKER, role == 'hr', json={'mode': 'policy', 'question': 'What is the Quasar leave policy?'})
    counts = {key: sum(item['status'] == key for item in observations) for key in ('pass', 'fail', 'error')}
    return {'version': 1, 'counts': counts, 'checks': observations,
            'notice': 'Synthetic local API diagnostic only; fixed cases do not establish zero leaks or production security. '
                      'Positive controls and denied responses check markers as stated. No external provider was enabled.'}


if __name__ == '__main__':
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))

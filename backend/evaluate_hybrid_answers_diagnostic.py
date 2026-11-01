"""Small synthetic, non-benchmark diagnostic for experimental policy answers.

Requires a dedicated PostgreSQL/pgvector database and local model, plus an
authorized model key in the environment. Only fictional policy text is sent.
"""
import json
import os
import re
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app

CASES = [
    ('remote', 'Can employees work from home?', True),
    ('approval', 'Is manager consent needed to work from home?', True),
    ('internet', 'Does working from home include paid internet?', False),
    ('equipment', 'Does working from home include a company laptop?', False),
    ('multipart', 'Can employees work from home and receive paid internet?', False),
    ('salary', 'How much salary increase is provided for working from home?', False),
    ('insurance', 'Does working from home include additional health insurance?', False),
    ('electricity', 'Does the company reimburse electricity used while working from home?', False),
    ('denial', 'Can managers deny a request to work from home without giving a reason?', False),
    ('childcare', 'Does working from home include a childcare allowance?', False),
    ('additional-days', 'How many days can staff do their jobs from home?', True),
    ('additional-approval', 'Must staff obtain approval before working remotely?', True),
    ('additional-permission', 'Does a supervisor need to permit work from home?', True),
    ('additional-ar-consent', '\u0647\u0644 \u064a\u062d\u062a\u0627\u062c \u0627\u0644\u0645\u0648\u0638\u0641 \u0625\u0644\u0649 \u0645\u0648\u0627\u0641\u0642\u0629 \u0627\u0644\u0645\u062f\u064a\u0631 \u0644\u0644\u0639\u0645\u0644 \u0645\u0646 \u0627\u0644\u0645\u0646\u0632\u0644\u061f', True),
    ('additional-hours', 'What are the working hours for staff working from home?', False),
    ('additional-transport', 'Is commuting reimbursement provided to remote workers?', False),
    ('additional-salary', 'Does the manager grant extra salary for working remotely?', False),
    ('additional-ar-equipment', '\u0647\u0644 \u062a\u0648\u0641\u0631 \u0627\u0644\u0634\u0631\u0643\u0629 \u062d\u0627\u0633\u0648\u0628\u0627 \u0644\u0644\u0639\u0645\u0644 \u0645\u0646 \u0627\u0644\u0645\u0646\u0632\u0644\u061f', False),
]


def run_selector_only():
    """Measure model selection using one supplied passage, without vector retrieval."""
    from app.knowledge import Source
    from app.policy_agent import _grounded_quotes, _request_quote_selection

    source = Source(id='synthetic-policy', title='Remote work',
                    text='Staff may work remotely two days per week with manager approval.')
    results = []
    for case_id, question, supported in CASES:
        try:
            selected = _request_quote_selection(question, [{'id': source.id, 'title': source.title, 'text': source.text}])
            answer = _grounded_quotes(selected, [source])
            results.append({'id': case_id, 'expected_supported': supported, 'returned_quote': bool(answer),
                            'answer': answer})
        except Exception as exc:  # noqa: BLE001 - diagnostic records failure, not secrets or provider output.
            results.append({'id': case_id, 'expected_supported': supported, 'error': type(exc).__name__})
    return results


def run():
    required = ('GROQ_API_KEY', 'TAWZEEF_EMBEDDING_MODEL_DIR', 'PGDATABASE')
    if any(not os.environ.get(name) for name in required) or os.environ.get('TAWZEEF_TEST_POSTGRES') != '1':
        raise RuntimeError('Requires an explicitly configured disposable database, local model, and model key')
    if not re.fullmatch(r'tawzeef_test_[a-f0-9]{32}', os.environ['PGDATABASE']):
        raise RuntimeError('Use a dedicated disposable test database')
    os.environ['TAWZEEF_ENABLE_GROQ'] = '1'
    os.environ['TAWZEEF_ENABLE_HYBRID'] = '1'
    os.environ['TAWZEEF_POLICY_HYBRID_ANSWERS'] = '1'
    os.environ.pop('TAWZEEF_POLICY_HYBRID_SUGGESTIONS', None)
    os.environ.pop('TAWZEEF_USE_POLICY_HYBRID', None)
    with TemporaryDirectory(prefix='policy-answer-diagnostic-') as tmp:
        path = Path(tmp) / 'diagnostic.sqlite3'
        with TestClient(create_app(path, seed_demo=True)) as client:
            assert client.post('/api/auth/login', json={'username': 'hr', 'password': 'hr-demo-only'}).status_code == 200
            document = client.post('/api/knowledge/documents/text', json={
                'title': 'Remote work', 'text': 'Staff may work remotely two days per week with manager approval.',
                'audience': 'staff', 'collection': 'handbook',
            })
            assert document.status_code == 201
            with connect(path) as db:
                db.execute('UPDATE knowledge_settings SET include_sample_policies=0 WHERE id=1')
            assert client.post('/api/auth/login', json={'username': 'employee', 'password': 'employee-demo-only'}).status_code == 200
            conversation = client.post('/api/conversations', json={'title': 'Diagnostic'}).json()['id']
            results = []
            for case_id, question, supported in CASES:
                response = client.post(f'/api/conversations/{conversation}/messages',
                                       json={'question': question, 'mode': 'policy'})
                response.raise_for_status()
                answer = response.json()
                results.append({'id': case_id, 'expected_supported': supported,
                                'mode': answer['mode'], 'answer': answer['answer'], 'notice': answer['notice']})
    return results


if __name__ == '__main__':
    print(json.dumps(run_selector_only() if '--selector-only' in sys.argv else run(), ensure_ascii=False, indent=2))

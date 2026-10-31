"""Synthetic API diagnostic for the actual policy-conversation workflow.

This is not answer accuracy, a human benchmark, or a production evaluation.
It uses temporary local storage, draft labels, and no external provider.
"""
import hashlib
import json
import os
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app

ROOT = Path(__file__).resolve().parent.parent / 'data' / 'capstone_samples'
FILES = ('handbook.json', 'policy-qa.json', 'negative-qa.json')


@contextmanager
def isolated_providers():
    keys = ('TAWZEEF_ENABLE_GROQ', 'TAWZEEF_ENABLE_HYBRID', 'TAWZEEF_USE_POLICY_HYBRID')
    old = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ[key] = '0'
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def expect(response, code):
    if response.status_code != code:
        raise RuntimeError(f'API request failed with HTTP {response.status_code}; expected {code}')
    return response.json()


def evaluate(root: Path = ROOT) -> dict:
    payloads = {name: (root / name).read_bytes() for name in FILES}
    corpus = {name: json.loads(raw) for name, raw in payloads.items()}
    sections = corpus['handbook.json']['sections']
    questions = corpus['policy-qa.json']['cases']
    negatives = corpus['negative-qa.json']['cases']
    if len({section['id'] for section in sections}) != len(sections):
        raise ValueError('Duplicate section ID')
    with isolated_providers(), TemporaryDirectory(prefix='tawzeef-policy-api-') as temp:
        path = Path(temp) / 'evaluation.sqlite3'
        with TestClient(create_app(path, seed_demo=True)) as client:
            def login(role):
                expect(client.post('/api/auth/login', json={'username': role, 'password': role + '-demo-only'}), 200)

            login('hr')
            # Disable seeded examples so returned sources come only from this corpus.
            with connect(path) as db:
                db.execute('UPDATE knowledge_settings SET include_sample_policies=0 WHERE id=1')
            section_sources = {}
            section_docids = {}
            for section in sections:
                if section['document_type'] != 'handbook':
                    raise ValueError('Non-handbook section in handbook corpus')
                doc = expect(client.post('/api/knowledge/documents/text', json={
                    'title': f"{section['title']} {section['title_ar']}",
                    'text': f"{section['text_en']}\n{section['text_ar']}",
                    'collection': 'handbook', 'audience': section['audience'],
                }), 201)
                section_docids[section['id']] = doc['id']
                section_sources[section['id']] = f"db-doc-{doc['id']}-p1"
            chats = {}
            for role in ('candidate', 'employee', 'hr'):
                login(role)
                chats[role] = expect(client.post('/api/conversations', json={'title': 'Synthetic diagnostic'}), 201)['id']

            def ask(case):
                login(case['role'])
                answer = expect(client.post(f"/api/conversations/{chats[case['role']]}/messages", json={
                    'mode': 'policy', 'question': case['question'],
                }), 201)
                return answer

            retrieval = []
            for case in questions:
                expected = [section_sources[ref] for ref in case['expected_section_ids']]
                answer = ask(case)
                actual = [source['id'] for source in answer['sources']]
                retrieval.append({'id': case['id'], 'language': case['language'], 'role': case['role'],
                                  'expected_ids': expected, 'returned_ids': actual, 'mode': answer['mode'],
                                  'recall_at_3': bool(set(expected).intersection(actual[:3]))})
            negative_results = []
            for case in negatives:
                answer = ask(case)
                actual = [source['id'] for source in answer['sources']]
                forbidden = [section_sources[ref] for ref in case['forbidden_section_ids']]
                negative_results.append({'id': case['id'], 'mode': answer['mode'], 'returned_ids': actual,
                                         'forbidden_returned': sorted(set(forbidden).intersection(actual)),
                                         'abstained': answer['mode'] == 'abstain'})

            # Explicit source-revocation test through HR's real deactivate API.
            login('employee')
            chat = expect(client.post('/api/conversations', json={'title': 'Revocation check'}), 201)['id']
            target = 'cap-policy-annual-leave'
            before = expect(client.post(f'/api/conversations/{chat}/messages', json={
                'mode': 'policy', 'question': 'annual leave',
            }), 201)
            source_id = section_sources[target]
            present_before = source_id in [item['id'] for item in before['sources']]
            login('hr')
            expect(client.post(f"/api/knowledge/documents/{section_docids[target]}/deactivate"), 200)
            login('employee')
            after = expect(client.get(f'/api/conversations/{chat}'), 200)['messages'][0]
            hidden = after['mode'] == 'unavailable' and source_id not in [s['id'] for s in after['sources']]
            original_text = next(s['text_en'] for s in sections if s['id'] == target)
            hidden = hidden and original_text not in after['answer']
            if not present_before:
                raise RuntimeError('Revocation diagnostic did not retrieve its target first')

    return {
        'disclaimer': 'Synthetic draft labels; no human review. Retrieval is not answer correctness or proof of access control.',
        'settings': {'entrypoint': 'POST /api/conversations/{id}/messages mode=policy',
                     'provider': 'disabled', 'hybrid': 'disabled', 'sources': 'synthetic handbook text documents',
                     'pages': 'one text document per logical section; no PDF pagination',
                     'sha256': {name: hashlib.sha256(raw).hexdigest() for name, raw in payloads.items()}},
        'summary': {'recall_at_3': sum(item['recall_at_3'] for item in retrieval), 'total': len(retrieval),
                    'modes': dict(Counter(item['mode'] for item in retrieval)),
                    'negative_abstained': sum(item['abstained'] for item in negative_results),
                    'negative_total': len(negative_results),
                    'negative_forbidden_returned': sum(bool(item['forbidden_returned']) for item in negative_results),
                    'revoked_source_hidden': hidden},
        'retrieval': retrieval, 'negative_checks': negative_results,
    }


if __name__ == '__main__':
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))

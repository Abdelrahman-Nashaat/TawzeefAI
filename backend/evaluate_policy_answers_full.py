"""Run a synthetic policy-conversation diagnostic; answers need human review.

Requires a dedicated disposable PostgreSQL database, pinned local embeddings,
and an authorized model key. Only bundled fictional policies are transmitted.
"""
import hashlib
import json
import os
import re
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql

from app import policy_agent
from app.db import connect
from app.main import create_app
from app.rag_postgres_evaluation import runtime_hashes
from evaluate_policy_api import FILES, ROOT, expect


def runtime_snapshot():
    result = runtime_hashes()
    for path in (Path(__file__), *(Path(__file__).parent / 'app' / name for name in
            ('policy_agent.py', 'conversations.py', 'ai_provider.py'))):
        result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


@contextmanager
def observe_calls():
    """Record call outcomes without changing behavior or logging exceptions."""
    names = ('_request_search', '_translate_retrieval_query', '_policy_hybrid', '_request_quote_selection')
    original = {name: getattr(policy_agent, name) for name in names}
    events = []
    try:
        for name, func in original.items():
            def wrapper(*args, _name=name, _func=func, **kwargs):
                try:
                    value = _func(*args, **kwargs)
                    events.append({'stage': _name, 'status': 'ok'})
                    return value
                except Exception:
                    events.append({'stage': _name, 'status': 'error'})
                    raise
            setattr(policy_agent, name, wrapper)
        yield events
    finally:
        for name, func in original.items():
            setattr(policy_agent, name, func)


def summarize(rows):
    """Summarize observable events, never infer factual answer correctness."""
    supported = [row for row in rows if row['case_kind'] == 'draft_answerable']
    negative = [row for row in rows if row['case_kind'] == 'draft_negative']
    return {
        'total': len(rows),
        'draft_answerable': len(supported),
        'expected_source_returned': sum(row['expected_source_returned'] for row in supported),
        'negative_cases': len(negative),
        'negative_clean_abstentions': sum(row['mode'] == 'abstain' and not row['call_error'] for row in negative),
        'forbidden_source_returned': sum(bool(row['forbidden_returned']) for row in negative),
        'call_error_cases': sum(row['call_error'] for row in rows),
        'modes': dict(Counter(row['mode'] for row in rows)),
        'answer_correctness': 'not assessed; independent human review required',
    }


def run(model_dir: Path):
    if os.environ.get('TAWZEEF_TEST_POSTGRES') != '1' or not os.environ.get('GROQ_API_KEY'):
        raise RuntimeError('Requires an explicit disposable PostgreSQL database and configured provider')
    payloads = {name: (ROOT / name).read_bytes() for name in FILES}
    corpus = {name: json.loads(data) for name, data in payloads.items()}
    sections = corpus['handbook.json']['sections']
    if len({section['id'] for section in sections}) != len(sections):
        raise ValueError('Duplicate section IDs')
    schema = 'policy_answers_' + uuid.uuid4().hex
    env = {'PGOPTIONS': f'-c search_path={schema},public', 'TAWZEEF_ENABLE_GROQ': '1',
           'TAWZEEF_ENABLE_HYBRID': '1', 'TAWZEEF_EMBEDDING_MODEL_DIR': str(model_dir.resolve()),
           'TAWZEEF_POLICY_HYBRID_ANSWERS': '1', 'TAWZEEF_POLICY_TRANSLATED_RETRIEVAL': '1',
           'TAWZEEF_POLICY_HYBRID_SUGGESTIONS': '0', 'TAWZEEF_USE_POLICY_HYBRID': '0'}
    old = {key: os.environ.get(key) for key in env}
    before = runtime_snapshot()
    try:
        with psycopg.connect(autocommit=True, connect_timeout=5) as connection:
            db_name = connection.execute('SELECT current_database()').fetchone()[0]
            if not re.fullmatch(r'tawzeef_test_[a-f0-9]{32}', db_name):
                raise RuntimeError('Use a dedicated disposable tawzeef_test_<32 hex> database')
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            try:
                connection.execute(sql.SQL('''CREATE TABLE {}.tawzeef_vectors (
                    namespace text NOT NULL, model text NOT NULL, source_id text NOT NULL,
                    revision text NOT NULL CHECK (length(revision)=64),
                    embedding public.vector(384) NOT NULL, PRIMARY KEY (namespace,model,source_id)
                )''').format(sql.Identifier(schema)))
                os.environ.update(env)
                with psycopg.connect(autocommit=True) as probe:
                    if probe.execute('SELECT current_schema()').fetchone()[0] != schema:
                        raise RuntimeError('Evaluation schema was not selected')
                with TemporaryDirectory(prefix='tawzeef-policy-answer-eval-') as tmp:
                    path = Path(tmp) / 'eval.sqlite3'
                    with observe_calls() as events, TestClient(create_app(path, seed_demo=True)) as client:
                        def login(role):
                            expect(client.post('/api/auth/login', json={
                                'username': role, 'password': role + '-demo-only'}), 200)

                        login('hr')
                        with connect(path) as db:
                            db.execute('UPDATE knowledge_settings SET include_sample_policies=0 WHERE id=1')
                        ids = {}
                        for section in sections:
                            if section['document_type'] != 'handbook':
                                raise ValueError('Non-handbook section in dataset')
                            document = expect(client.post('/api/knowledge/documents/text', json={
                                'title': f"{section['title']} {section['title_ar']}",
                                'text': f"{section['text_en']}\n{section['text_ar']}",
                                'collection': 'handbook', 'audience': section['audience']}), 201)
                            ids[section['id']] = f"db-doc-{document['id']}-p1"
                        chats = {}
                        for role in ('candidate', 'employee', 'hr'):
                            login(role)
                            chats[role] = expect(client.post('/api/conversations', json={'title': 'Synthetic review'}), 201)['id']
                        rows = []
                        cases = [('draft_answerable', c) for c in corpus['policy-qa.json']['cases']]
                        cases += [('draft_negative', c) for c in corpus['negative-qa.json']['cases']]
                        for kind, case in cases:
                            login(case['role'])
                            events.clear()
                            response = expect(client.post(f"/api/conversations/{chats[case['role']]}/messages", json={
                                'mode': 'policy', 'question': case['question']}), 201)
                            actual = [source['id'] for source in response['sources']]
                            expected = [ids[ref] for ref in case.get('expected_section_ids', [])]
                            forbidden = [ids[ref] for ref in case.get('forbidden_section_ids', [])]
                            rows.append({'id': case['id'], 'case_kind': kind, 'role': case['role'],
                                'language': case.get('language', 'unspecified'), 'question': case['question'],
                                'draft_expected_answer': case.get('expected_answer'),
                                'expected_ids': expected, 'returned_ids': actual,
                                'expected_source_returned': bool(set(expected).intersection(actual[:3])),
                                'forbidden_returned': sorted(set(forbidden).intersection(actual)),
                                'mode': response['mode'], 'answer': response['answer'], 'notice': response['notice'],
                                'calls': list(events), 'call_error': any(e['status'] == 'error' for e in events),
                                'human_correctness': None})
            finally:
                connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    after = runtime_snapshot()
    if after != before:
        raise RuntimeError('Runtime files changed during evaluation')
    return {'notice': 'Synthetic draft cases; expected-source retrieval is not answer correctness. '
                      'Provider errors are recorded separately; human correctness has not been assessed.',
            'dataset_sha256': {name: hashlib.sha256(raw).hexdigest() for name, raw in payloads.items()},
            'runtime_sha256': before, 'summary': summarize(rows), 'cases': rows}


if __name__ == '__main__':
    model_path = Path(__file__).resolve().parent.parent / '.tooling' / 'multilingual-e5-small'
    print(json.dumps(run(model_path), ensure_ascii=False, indent=2))

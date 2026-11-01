"""Compare policy conversation and experimental hybrid retrieval on draft cases.

Run only with a dedicated disposable PostgreSQL database with pgvector.
The candidate endpoint does not produce an answer and is not the policy agent.
"""
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql

from app.db import connect
from app.main import create_app
from app.rag_postgres_evaluation import runtime_hashes
from evaluate_policy_api import FILES, ROOT, evaluate, expect


def summarize(positive, negatives):
    groups = {}
    for row in positive:
        group = groups.setdefault(row['language'], {'total': 0, 'lexical_hits': 0, 'hybrid_hits': 0})
        group['total'] += 1
        group['lexical_hits'] += int(row['lexical_hit'])
        group['hybrid_hits'] += int(row['hybrid_hit'])
    return {'by_language': groups, 'negative_total': len(negatives),
            'hybrid_empty': sum(not row['hybrid_ids'] for row in negatives),
            'hybrid_forbidden_returned': sum(bool(row['forbidden_returned']) for row in negatives)}


def run(model_dir: Path):
    if os.environ.get('TAWZEEF_TEST_POSTGRES') != '1':
        raise RuntimeError('Set TAWZEEF_TEST_POSTGRES=1 and use a dedicated disposable test database')
    payloads = {name: (ROOT / name).read_bytes() for name in FILES}
    corpus = {name: json.loads(raw) for name, raw in payloads.items()}
    sections = corpus['handbook.json']['sections']
    if len({s['id'] for s in sections}) != len(sections):
        raise ValueError('Duplicate section IDs')
    hashes_before = runtime_hashes()
    baseline = evaluate()
    if baseline['settings']['sha256'] != {name: hashlib.sha256(raw).hexdigest() for name, raw in payloads.items()}:
        raise RuntimeError('Dataset changed during evaluation')
    baseline_by_id = {row['id']: row for row in baseline['retrieval']}
    schema = 'tawzeef_eval_' + uuid.uuid4().hex
    names = ('PGOPTIONS', 'TAWZEEF_ENABLE_HYBRID', 'TAWZEEF_EMBEDDING_MODEL_DIR', 'TAWZEEF_ENABLE_GROQ')
    previous = {name: os.environ.get(name) for name in names}
    try:
        with psycopg.connect(autocommit=True) as connection:
            database = connection.execute('SELECT current_database()').fetchone()[0]
            if not re.fullmatch(r'tawzeef_test_[a-f0-9]{32}', database):
                raise RuntimeError('Database must be named tawzeef_test_<32 lowercase hexadecimal characters>')
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            try:
                os.environ['PGOPTIONS'] = f'-c search_path={schema},public'
                connection.execute(sql.SQL('''CREATE TABLE {}.tawzeef_vectors (
                    namespace text NOT NULL, model text NOT NULL, source_id text NOT NULL,
                    revision text NOT NULL CHECK (length(revision) = 64),
                    embedding public.vector(384) NOT NULL, PRIMARY KEY (namespace, model, source_id)
                )''').format(sql.Identifier(schema)))
                with psycopg.connect(autocommit=True) as probe:
                    if probe.execute('SELECT current_schema()').fetchone()[0] != schema:
                        raise RuntimeError('Evaluation schema was not selected')
                os.environ['TAWZEEF_ENABLE_HYBRID'] = '1'
                os.environ['TAWZEEF_EMBEDDING_MODEL_DIR'] = str(model_dir.resolve())
                os.environ['TAWZEEF_ENABLE_GROQ'] = '0'
                with TemporaryDirectory(prefix='tawzeef-policy-hybrid-') as tmp:
                    path = Path(tmp) / 'knowledge.sqlite3'
                    with TestClient(create_app(path, seed_demo=True)) as client:
                        def login(role):
                            expect(client.post('/api/auth/login', json={'username': role, 'password': role + '-demo-only'}), 200)

                        login('hr')
                        with connect(path) as db:
                            db.execute('UPDATE knowledge_settings SET include_sample_policies=0 WHERE id=1')
                        source_ids = {}
                        for section in sections:
                            if section['document_type'] != 'handbook':
                                raise ValueError('Non-handbook section in corpus')
                            doc = expect(client.post('/api/knowledge/documents/text', json={
                                'title': f"{section['title']} {section['title_ar']}",
                                'text': f"{section['text_en']}\n{section['text_ar']}",
                                'collection': 'handbook', 'audience': section['audience'],
                            }), 201)
                            source_ids[section['id']] = f"db-doc-{doc['id']}-p1"

                        def search(case):
                            login(case['role'])
                            result = expect(client.post('/api/knowledge/experimental-hybrid', json={
                                'question': case['question'], 'collection': 'handbook'}), 200)
                            return [source['id'] for source in result['sources']]

                        positive = []
                        for case in corpus['policy-qa.json']['cases']:
                            actual = search(case)
                            expected = [source_ids[ref] for ref in case['expected_section_ids']]
                            positive.append({'id': case['id'], 'language': case['language'], 'role': case['role'],
                                'lexical_hit': baseline_by_id[case['id']]['recall_at_3'],
                                'hybrid_hit': bool(set(expected).intersection(actual[:3])),
                                'hybrid_ids': actual})
                        negatives = []
                        for case in corpus['negative-qa.json']['cases']:
                            actual = search(case)
                            forbidden = {source_ids[ref] for ref in case['forbidden_section_ids']}
                            negatives.append({'id': case['id'], 'hybrid_ids': actual,
                                              'forbidden_returned': sorted(forbidden.intersection(actual))})
            finally:
                connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    if runtime_hashes() != hashes_before:
        raise RuntimeError('Runtime files changed during evaluation')
    return {'notice': 'Synthetic development cases with draft labels; candidates are not verified answers.',
            'dataset_sha256': baseline['settings']['sha256'], 'runtime_sha256': hashes_before,
            'baseline_summary': baseline['summary'], 'summary': summarize(positive, negatives),
            'positive_cases': positive, 'negative_cases': negatives}


if __name__ == '__main__':
    model = Path(__file__).resolve().parent.parent / '.tooling' / 'multilingual-e5-small'
    report = run(model)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report['summary']['hybrid_forbidden_returned']:
        raise SystemExit(1)

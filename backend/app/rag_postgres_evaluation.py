"""Diagnostic of the authenticated hybrid endpoint, real model and pgvector.

Only fictional data is inserted into a temporary SQLite database and a uniquely
named PostgreSQL schema. Use an explicitly dedicated disposable test database.
The results measure retrieval candidates, not answer correctness or fairness.
"""

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import tempfile
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql

from app import hybrid_api
from app.db import connect
from app.main import create_app
from app.semantic_experiment import MODEL_SHA256, TOKENIZER_SHA256, LocalE5

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / 'data' / 'fixtures' / 'rag-postgres-cases.json'
ALLOWED = {'candidate': {'public'}, 'employee': {'public', 'staff'}, 'hr': {'public', 'staff', 'hr'}}
RUNTIME_FILES = ('auth.py', 'hybrid_api.py', 'knowledge.py', 'knowledge_documents.py',
                 'postgres_retrieval.py', 'postgres_vectors.py', 'semantic_experiment.py')


def runtime_hashes():
    root = ROOT / 'backend' / 'app'
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in RUNTIME_FILES}


class RecordingModel:
    def __init__(self, delegate):
        self.delegate = delegate
        self.passages = []

    def encode(self, texts, *, query):
        if not query:
            self.passages.extend(texts)
        return self.delegate.encode(texts, query=query)


def summarize(cases):
    """Count each result against its independent reference and language."""
    groups = defaultdict(lambda: {'hits': 0, 'top1': 0, 'total': 0})
    negatives = {'empty': 0, 'total': 0}
    errors = 0
    for case in cases:
        if case.get('error_http') is not None:
            errors += 1
            continue
        if case['expected']:
            group = groups[case['language']]
            group['total'] += 1
            group['hits'] += case['support_retrieved']
            group['top1'] += case['top1_support']
        else:
            negatives['total'] += 1
            negatives['empty'] += not case['returned']
    return {'support_by_language': dict(groups), 'no_support_no_result': negatives,
            'http_errors': errors,
            'authorization_violations': sum(bool(c.get('authorization_violation')) for c in cases),
            'citation_violations': sum(bool(c.get('citation_violation')) for c in cases)}


def revision():
    try:
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, check=True)
        state = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT, capture_output=True, text=True, check=True)
        return {'head': head.stdout.strip(), 'worktree_dirty': bool(state.stdout.strip())}
    except (OSError, subprocess.CalledProcessError):
        return {'head': None, 'worktree_dirty': None}


def run(model_dir: Path):
    if os.environ.get('TAWZEEF_TEST_POSTGRES') != '1':
        raise RuntimeError('Set TAWZEEF_TEST_POSTGRES=1 and configure a dedicated disposable test database')
    raw = DATASET.read_bytes()
    dataset = json.loads(raw)
    docs = dataset['documents']
    if len({d['title'] for d in docs}) != len(docs) or len({c['id'] for c in dataset['cases']}) != len(dataset['cases']):
        raise ValueError('Duplicate fixture titles or case IDs')
    by_title = {doc['title']: doc for doc in docs}
    for case in dataset['cases']:
        expected = case['expected']
        if case['role'] not in ALLOWED:
            raise ValueError('Invalid role')
        if expected:
            doc = by_title.get(expected['title'])
            if not doc or not doc['active'] or doc['audience'] not in ALLOWED[case['role']] or not 1 <= expected['page'] <= len(doc['pages']):
                raise ValueError('Invalid or unauthorized expected document')

    # PGOPTIONS affects the separate connection created by the HTTP endpoint.
    schema = 'tawzeef_eval_' + uuid.uuid4().hex
    previous = {key: os.environ.get(key) for key in ('PGOPTIONS', 'TAWZEEF_ENABLE_HYBRID', 'TAWZEEF_EMBEDDING_MODEL_DIR', 'TAWZEEF_ENABLE_GROQ')}
    original_loader = hybrid_api._load_model
    before = runtime_hashes()
    recording = RecordingModel(LocalE5(model_dir))
    results = []
    try:
        with psycopg.connect(autocommit=True) as connection:
            database = connection.execute('SELECT current_database()').fetchone()[0]
            if not re.fullmatch(r'tawzeef_test_[a-f0-9]{32}', database):
                raise RuntimeError('The database name must match the dedicated disposable tawzeef_test_<uuid> format')
            connection.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
            try:
                os.environ['PGOPTIONS'] = f'-c search_path={schema},public'
                # Create the evaluation table with an explicit schema. This
                # prevents unqualified app SQL from falling through to a
                # similarly named table in the public schema.
                connection.execute(sql.SQL('''CREATE TABLE {}.tawzeef_vectors (
                    namespace text NOT NULL, model text NOT NULL, source_id text NOT NULL,
                    revision text NOT NULL CHECK (length(revision) = 64),
                    embedding public.vector(384) NOT NULL, PRIMARY KEY (namespace, model, source_id)
                )''').format(sql.Identifier(schema)))
                with psycopg.connect(autocommit=True) as probe:
                    if probe.execute('SELECT current_schema()').fetchone()[0] != schema:
                        raise RuntimeError('PostgreSQL did not select the isolated evaluation schema')
                os.environ['TAWZEEF_ENABLE_HYBRID'] = '1'
                os.environ['TAWZEEF_EMBEDDING_MODEL_DIR'] = str(model_dir.resolve())
                os.environ['TAWZEEF_ENABLE_GROQ'] = '0'
                hybrid_api._load_model = lambda directory: recording
                with tempfile.TemporaryDirectory(prefix='tawzeef-rag-eval-') as tmp:
                    path = Path(tmp) / 'knowledge.sqlite3'
                    with TestClient(create_app(path, seed_demo=True)) as client:
                        with connect(path) as db:
                            hr = db.execute("SELECT id FROM users WHERE username='hr'").fetchone()[0]
                            for doc in docs:
                                pages = [{'page': i, 'text': text} for i, text in enumerate(doc['pages'], 1)]
                                db.execute('''INSERT INTO knowledge_documents(title,audience,active,text,created_by,pages_json)
                                    VALUES(?,?,?,?,?,?)''', (doc['title'], doc['audience'], int(doc['active']),
                                    '\n'.join(doc['pages']), hr, json.dumps(pages, ensure_ascii=False)))
                        for case in dataset['cases']:
                            role = case['role']
                            login = client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'})
                            if login.status_code != 200:
                                raise RuntimeError(f"Synthetic login failed: {role}")
                            recording.passages.clear()
                            started = time.perf_counter()
                            response = client.post('/api/knowledge/experimental-hybrid', json={'question': case['question']})
                            elapsed = round((time.perf_counter() - started) * 1000, 1)
                            item = {**case, 'latency_ms': elapsed}
                            if response.status_code != 200:
                                item['error_http'] = response.status_code
                                results.append(item)
                                continue
                            body = response.json()
                            returned = body['sources']
                            unauthorized = [doc for doc in docs if not doc['active'] or doc['audience'] not in ALLOWED[role]]
                            violation = any(doc['title'] == source['title'] for doc in unauthorized for source in returned)
                            violation |= any(page in text for doc in unauthorized for page in doc['pages'] for text in recording.passages)
                            citation_violation = any(source['title'] not in by_title or
                                type(source.get('page')) is not int or not 1 <= source['page'] <= len(by_title[source['title']]['pages']) or
                                source['text'] not in by_title[source['title']]['pages'][source['page'] - 1] for source in returned)
                            expected = case['expected']
                            hits = [source['title'] == expected['title'] and source['page'] == expected['page'] for source in returned] if expected else []
                            item.update(returned=returned, status=body['status'], support_retrieved=any(hits),
                                        top1_support=bool(hits and hits[0]), authorization_violation=bool(violation),
                                        citation_violation=bool(citation_violation))
                            results.append(item)
            finally:
                connection.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
    finally:
        hybrid_api._load_model = original_loader
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    if runtime_hashes() != before:
        raise RuntimeError('Runtime files changed during evaluation; rerun after changes settle')
    return {'created_utc': datetime.now(UTC).isoformat(), 'revision': revision(),
            'platform': platform.platform(), 'python': platform.python_version(),
            'dataset_version': dataset['version'], 'dataset_sha256': hashlib.sha256(raw).hexdigest(),
            'model_sha256_expected': MODEL_SHA256, 'tokenizer_sha256_expected': TOKENIZER_SHA256,
            'runtime_sha256': before,
            'method': 'Real authenticated experimental-hybrid endpoint, local pinned model and PostgreSQL/pgvector',
            'notice': 'Small synthetic development set. No generated answers, real users or fairness claims.',
            'summary': summarize(results), 'cases': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, default=ROOT / '.tooling' / 'multilingual-e5-small')
    args = parser.parse_args()
    report = run(args.model_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    summary = report['summary']
    if summary['authorization_violations'] or summary['citation_violations'] or summary['http_errors']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()

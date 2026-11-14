"""Synthetic diagnostic of the PostgreSQL demo knowledge endpoint.

Requires a disposable database named tawzeef_test_<32 lowercase hex digits>,
pgvector and a separately provisioned pinned local E5 model. Only a randomly
named experimental schema is created and removed. No external model is called.
"""
import argparse
import hashlib
import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from fastapi.testclient import TestClient
from psycopg import sql

from app import postgres_knowledge
from app.knowledge import _document_passages
from app.postgres_experimental import create_postgres_app
from app.semantic_experiment import MODEL_SHA256, TOKENIZER_SHA256, LocalE5

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / 'data' / 'fixtures' / 'rag-postgres-cases.json'
AUDIENCES = {'candidate': {'public'}, 'employee': {'public', 'staff'},
             'hr': {'public', 'staff', 'hr'}}
RUNTIME_FILES = ('rag_pg_demo_evaluation.py', 'postgres_experimental.py', 'postgres_knowledge.py',
                 'knowledge.py', 'semantic_experiment.py', 'embedding_identity.py')


def runtime_hashes():
    return {name: hashlib.sha256((ROOT / 'backend' / 'app' / name).read_bytes()).hexdigest()
            for name in RUNTIME_FILES}


class RecordingModel:
    def __init__(self, delegate):
        self.delegate = delegate
        self.passages = []

    def encode(self, texts, *, query):
        if not query:
            self.passages.extend(texts)
        return self.delegate.encode(texts, query=query)


def summarize(results):
    summary = {}
    for mode in ('lexical', 'hybrid'):
        cases = [result for result in results if result['mode'] == mode]
        positives = [case for case in cases if case['expected'] is not None]
        negatives = [case for case in cases if case['expected'] is None]
        summary[mode] = {
            'answerable_cases': len(positives),
            'expected_in_excerpts': sum(case['expected_in_excerpts'] for case in positives),
            'expected_at_first_excerpt': sum(case['expected_at_first_excerpt'] for case in positives),
            'expected_in_suggestions': sum(case['expected_in_suggestions'] for case in positives),
            'unsupported_cases': len(negatives),
            'unsupported_abstentions': sum(case['abstained'] for case in negatives),
            'unsupported_with_suggestions': sum(bool(case['suggestions']) for case in negatives),
            'authorization_violations': sum(case['authorization_violation'] for case in cases),
            'filter_violations': sum(case['filter_violation'] for case in cases),
            'citation_violations': sum(case['citation_violation'] for case in cases),
            'http_errors': sum(case['http_status'] != 200 for case in cases),
        }
    return summary


def analyze(case, mode, response, documents, elapsed, embedded=()):
    body = response.json() if response.status_code == 200 else {}
    excerpts, suggestions = body.get('sources', []), body.get('suggestions', [])
    by_title = {document['title']: document for document in documents}
    all_returned = excerpts + suggestions
    def matches(source):
        return case['expected'] is not None and all(source.get(key) == value for key, value in case['expected'].items())
    def authorized(doc):
        return (doc['active'] and doc['audience'] in AUDIENCES[case['role']] and
                (doc.get('collection', 'handbook') != 'past_hire_patterns' or case['role'] == 'hr'))

    def matches_filters(doc):
        return all(case.get(key) is None or doc.get(key, 'handbook' if key == 'collection' else None) == case[key]
                   for key in ('collection', 'job_id', 'section'))

    def chunks(predicate):
        return {source.text for index, doc in enumerate(documents) if predicate(doc)
                for page in doc['pages']
                for source in _document_passages({'id': index, 'title': doc['title'], 'text': page})}

    violation = any(source.get('title') not in by_title or not authorized(by_title[source['title']])
                    for source in all_returned)
    forbidden_chunks = chunks(lambda doc: not authorized(doc))
    violation |= any(text in forbidden_chunks for text in embedded)
    filtered_out = chunks(lambda doc: authorized(doc) and not matches_filters(doc))
    filter_violation = any(
        source.get('title') in by_title and (
            not matches_filters(by_title[source['title']]) or
            any(source.get(key) != case[key] for key in ('collection', 'job_id', 'section')
                if case.get(key) is not None)) for source in all_returned)
    filter_violation |= any(text in filtered_out for text in embedded)
    citation = False
    for source in all_returned:
        doc = by_title.get(source.get('title'))
        page = source.get('page')
        if (not doc or type(page) is not int or not 1 <= page <= len(doc['pages']) or
                not source.get('text') or source['text'] not in doc['pages'][page - 1]):
            citation = True
    return {'id': case['id'], 'mode': mode, 'role': case['role'], 'language': case['language'],
            'expected': case['expected'], 'http_status': response.status_code,
            'elapsed_ms': round(elapsed * 1000, 1),
            'abstained': body.get('mode') == 'abstain' and not excerpts,
            'expected_in_excerpts': any(map(matches, excerpts)),
            'expected_at_first_excerpt': bool(excerpts and matches(excerpts[0])),
            'expected_in_suggestions': any(map(matches, suggestions)),
            'authorization_violation': violation, 'filter_violation': filter_violation, 'citation_violation': citation,
            'excerpts': excerpts, 'suggestions': suggestions}


def run(dsn, model_dir):
    if os.environ.get('TAWZEEF_TEST_POSTGRES') != '1':
        raise RuntimeError('Set TAWZEEF_TEST_POSTGRES=1 and provide a dedicated disposable database')
    raw = DATASET.read_bytes()
    runtime_before = runtime_hashes()
    fixture = json.loads(raw)
    documents, cases = fixture['documents'], fixture['cases']
    titles = {doc['title'] for doc in documents}
    if len(titles) != len(documents) or len({case['id'] for case in cases}) != len(cases):
        raise ValueError('Duplicate fixture IDs')
    by_title = {doc['title']: doc for doc in documents}
    for case in cases:
        if case['role'] not in AUDIENCES:
            raise ValueError('Invalid fixture role')
        expected = case['expected']
        if expected:
            doc = by_title.get(expected['title'])
            if (not doc or not doc['active'] or doc['audience'] not in AUDIENCES[case['role']] or
                    (doc.get('collection', 'handbook') == 'past_hire_patterns' and case['role'] != 'hr') or
                    any(case.get(key) is not None and doc.get(key, 'handbook' if key == 'collection' else None) != case[key]
                        for key in ('collection', 'job_id', 'section')) or
                    type(expected['page']) is not int or not 1 <= expected['page'] <= len(doc['pages'])):
                raise ValueError('Invalid or inaccessible expected source')
    model = RecordingModel(LocalE5(model_dir))
    schema = 'tawzeef_exp_' + uuid.uuid4().hex
    variables = {'TAWZEEF_DATABASE_ENGINE': 'postgres-experimental', 'TAWZEEF_POSTGRES_DSN': dsn,
                 'TAWZEEF_POSTGRES_SCHEMA': schema, 'TAWZEEF_POSTGRES_KNOWLEDGE_HYBRID': '1',
                 'TAWZEEF_EMBEDDING_MODEL_DIR': str(model_dir)}
    old = {key: os.environ.get(key) for key in variables}
    original_loader = postgres_knowledge._load_embedder
    results = []
    created_schema_oid = None

    def mark_created(oid):
        nonlocal created_schema_oid
        created_schema_oid = oid

    with psycopg.connect(dsn, autocommit=True) as db:
        name = db.execute('SELECT current_database()').fetchone()[0]
        if not re.fullmatch(r'tawzeef_test_[a-f0-9]{32}', name):
            raise RuntimeError('Database must be a dedicated disposable tawzeef_test_<uuid> database')
        if not db.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone():
            raise RuntimeError('The disposable database must have pgvector installed')
        try:
            os.environ.update(variables)
            postgres_knowledge._load_embedder = lambda _: model
            with TestClient(create_postgres_app(seed_demo=True, require_new_schema=True,
                                               on_schema_created=mark_created)) as client:
                def login(role):
                    response = client.post('/api/auth/login', json={'username': role, 'password': role + '-demo-only'})
                    if response.status_code != 200:
                        raise RuntimeError('Synthetic login failed')
                login('hr')
                for doc in documents:
                    for page, text in enumerate(doc['pages'], 1):
                        created = client.post('/api/knowledge/documents/text', json={
                            'title': doc['title'], 'text': text, 'audience': doc['audience'],
                            'collection': doc.get('collection', 'handbook'), 'page': page,
                            **{key: doc[key] for key in ('job_id', 'section') if key in doc}})
                        if created.status_code != 201:
                            raise RuntimeError('Synthetic document creation failed')
                        if not doc['active']:
                            result = client.post(f"/api/knowledge/documents/{created.json()['id']}/deactivate")
                            if result.status_code != 200:
                                raise RuntimeError('Synthetic document deactivation failed')
                for case in cases:
                    login(case['role'])
                    for mode in ('lexical', 'hybrid'):
                        model.passages.clear()
                        started = time.perf_counter()
                        response = client.post('/api/knowledge/answer', json={
                            'question': case['question'], 'experimental_hybrid': mode == 'hybrid',
                            **{key: case[key] for key in ('collection', 'job_id', 'section') if key in case}})
                        results.append(analyze(case, mode, response, documents, time.perf_counter() - started,
                                               model.passages))
        finally:
            postgres_knowledge._load_embedder = original_loader
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if created_schema_oid is not None:
                row = db.execute('SELECT oid FROM pg_namespace WHERE nspname=%s', (schema,)).fetchone()
                if row and row[0] == created_schema_oid:
                    db.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
    if runtime_hashes() != runtime_before:
        raise RuntimeError('Runtime files changed during evaluation; rerun after changes settle')
    return {'created_utc': datetime.now(UTC).isoformat(),
            'dataset': fixture['version'], 'dataset_sha256': hashlib.sha256(raw).hexdigest(),
            'model_sha256_expected': MODEL_SHA256, 'tokenizer_sha256_expected': TOKENIZER_SHA256,
            'runtime_sha256': runtime_before,
            'notice': 'Synthetic development diagnostic; excerpts and suggestions do not prove answer correctness.',
            'summary': summarize(results), 'cases': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', required=True, type=Path)
    args = parser.parse_args()
    dsn = os.environ.get('TAWZEEF_TEST_POSTGRES_DSN', '')
    if not dsn:
        raise RuntimeError('Set TAWZEEF_TEST_POSTGRES_DSN')
    report = run(dsn, args.model_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if any(group[key] for group in report['summary'].values()
           for key in ('authorization_violations', 'filter_violations', 'citation_violations', 'http_errors')):
        raise SystemExit(1)


if __name__ == '__main__':
    main()

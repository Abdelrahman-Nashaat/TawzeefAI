"""Run the fictional development set through the actual local HR endpoint.

This is a diagnostic, not a pass/fail or hiring-performance benchmark. It uses
an isolated temporary SQLite database and never loads real résumés.
"""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tempfile
import time
from collections import defaultdict
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from docx import Document
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app
from app.semantic_experiment import MODEL_SHA256, TOKENIZER_SHA256
from app.semantic_screening import MIN_SIMILARITY

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / 'data' / 'fixtures' / 'semantic-resume-cases.json'


def _revision() -> dict:
    try:
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True, check=True)
        status = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT, capture_output=True, text=True, check=True)
        return {'head': head.stdout.strip(), 'worktree_dirty': bool(status.stdout.strip())}
    except (OSError, subprocess.CalledProcessError):
        return {'head': None, 'worktree_dirty': None}


def _login(client: TestClient, role: str) -> None:
    response = client.post('/api/auth/login', json={'username': role, 'password': f'{role}-demo-only'})
    if response.status_code != 200:
        raise RuntimeError(f'Cannot log in as synthetic {role}: HTTP {response.status_code}')


def _resume_docx(text: str) -> bytes:
    document = Document()
    for line in text.splitlines():
        document.add_paragraph(line)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def run(model_dir: Path) -> dict:
    dataset_bytes = DATASET.read_bytes()
    dataset = json.loads(dataset_bytes)
    cases = dataset['cases']
    if len({case['id'] for case in cases}) != len(cases):
        raise ValueError('Dataset case IDs must be unique')
    os.environ['TAWZEEF_ENABLE_SEMANTIC_SCREENING'] = '1'
    os.environ['TAWZEEF_EMBEDDING_MODEL_DIR'] = str(model_dir.resolve())
    # Synthetic cases always use the explicitly enabled demo app and local model.
    # No Groq or network provider is needed or invoked.
    os.environ['TAWZEEF_ENABLE_GROQ'] = '0'
    results = []
    with tempfile.TemporaryDirectory(prefix='tawzeef-semantic-eval-') as directory:
        path = Path(directory) / 'evaluation.sqlite3'
        app = create_app(path, seed_demo=True)
        with TestClient(app) as client:
            # Each fictional case gets its own synthetic role and single criterion.
            with connect(path) as db:
                db.executemany('''INSERT INTO jobs(id,title,location,required_skills,preferred_skills,responsibilities)
                    VALUES (?, ?, ?, ?, ?, ?)''', [
                    (f'eval-{i}', f'Synthetic role {i}', 'Synthetic', '[]', '[]', '[]')
                    for i in range(len(cases))
                ])
            _login(client, 'candidate')
            application_ids = []
            for i, case in enumerate(cases):
                response = client.post('/api/applications', params={'job_id': f'eval-{i}', 'filename': 'synthetic.docx'},
                                       content=_resume_docx(case['resume']))
                if response.status_code != 201:
                    raise RuntimeError(f"{case['id']}: synthetic upload HTTP {response.status_code}")
                application_ids.append(response.json()['id'])
            forbidden_candidate = client.post(f'/api/applications/{application_ids[0]}/rubric/suggestions').status_code
            _login(client, 'employee')
            forbidden_employee = client.post(f'/api/applications/{application_ids[0]}/rubric/suggestions').status_code
            _login(client, 'hr')
            for i, case in enumerate(cases):
                created = client.post(f'/api/jobs/eval-{i}/rubric/criteria',
                                      json={'category': 'mandatory', 'label': case['criterion']})
                if created.status_code != 201:
                    raise RuntimeError(f"{case['id']}: criterion HTTP {created.status_code}")
                app_id = application_ids[i]
                source_response = client.get(f'/api/applications/{app_id}/rubric/source')
                if source_response.status_code != 200:
                    raise RuntimeError(f"{case['id']}: source HTTP {source_response.status_code}")
                source = source_response.json()['text']
                start = time.perf_counter()
                response = client.post(f'/api/applications/{app_id}/rubric/suggestions')
                elapsed = round((time.perf_counter() - start) * 1000, 1)
                if response.status_code != 200:
                    results.append({'id': case['id'], 'language': case['language'], 'label': case['label'],
                                    'error_http': response.status_code, 'latency_ms': elapsed})
                    continue
                body = response.json()
                criteria = body['criteria']
                if len(criteria) != 1 or criteria[0]['criterion_ref'] != created.json()['criterion_ref']:
                    raise RuntimeError(f"{case['id']}: unexpected criterion in response")
                if body['source_sha256'] != hashlib.sha256(source.encode('utf-8')).hexdigest():
                    raise RuntimeError(f"{case['id']}: source digest mismatch")
                candidates = criteria[0]['candidates']
                if any(source[c['start']:c['end']] != c['excerpt'] for c in candidates):
                    raise RuntimeError(f"{case['id']}: candidate offsets do not match the redacted source")
                excerpts = [item['excerpt'] for item in candidates]
                results.append({
                    'id': case['id'], 'language': case['language'], 'label': case['label'],
                    'pair_id': case.get('pair_id'), 'returned': candidates,
                    'support_hit': any(expected in actual for expected in case['supporting_spans'] for actual in excerpts),
                    'qualifying_context_hit': any(expected in actual for expected in case['qualifying_spans'] for actual in excerpts),
                    'latency_ms': elapsed,
                })
    groups = defaultdict(lambda: {'hits': 0, 'total': 0})
    for result in results:
        if result['label'] == 'support':
            groups[result['language']]['total'] += 1
            groups[result['language']]['hits'] += bool(result.get('support_hit'))
    no_support = [result for result in results if result['label'] == 'no_support']
    pair = [result for result in results if result.get('pair_id') == 'identity-pair-1']
    return {
        'created_utc': datetime.now(UTC).isoformat(),
        'revision': _revision(), 'platform': platform.platform(), 'python': platform.python_version(),
        'dataset_version': dataset['version'], 'dataset_sha256': hashlib.sha256(dataset_bytes).hexdigest(),
        'model_sha256_expected': MODEL_SHA256, 'tokenizer_sha256_expected': TOKENIZER_SHA256,
        'minimum_similarity': MIN_SIMILARITY, 'k': 3,
        'support_recall_by_language': dict(groups),
        'no_support_no_result': {'count': sum(not row.get('returned') and 'error_http' not in row for row in no_support),
                                 'total': len(no_support)},
        'identity_pair_identical_candidates': (len(pair) == 2 and all('returned' in row for row in pair)
                                               and pair[0]['returned'] == pair[1]['returned']),
        'candidate_http': forbidden_candidate, 'employee_http': forbidden_employee,
        'cases': results,
        'notice': 'Synthetic development diagnostic only; not real-role accuracy, fairness, or evidence of ability.',
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, default=ROOT / '.tooling' / 'multilingual-e5-small')
    args = parser.parse_args()
    print(json.dumps(run(args.model_dir), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

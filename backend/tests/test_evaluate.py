"""Contract checks for the synthetic smoke-evaluation command."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import evaluate


@pytest.fixture
def synthetic_data(tmp_path, monkeypatch):
    role_dir = tmp_path / 'representative_role'
    policy_dir = tmp_path / 'fixtures'
    role_dir.mkdir()
    policy_dir.mkdir()
    (role_dir / 'role.json').write_text(json.dumps({
        'source_url': 'https://example.invalid/synthetic-role',
        'required_skills': ['Python', 'SQL'], 'preferred_skills': [],
    }), encoding='utf-8')
    cases = {
        'profiles': [
            {'id': 'strong', 'resume': 'Built Python services and SQL databases.'},
            {'id': 'weak', 'resume': 'Built Python services.'},
            {'id': 'equal', 'resume': 'Built Python services and SQL databases.'},
        ],
        'cases': [
            {'id': 'rank', 'check_kind': 'pairwise', 'candidate_ids': ['strong', 'weak'], 'expected_stronger': 'strong'},
            {'id': 'tie', 'check_kind': 'counterfactual_equivalence', 'candidate_ids': ['strong', 'equal']},
            {'id': 'uncertain', 'check_kind': 'insufficient_evidence', 'candidate_ids': ['weak']},
        ],
    }
    case_path = role_dir / 'cases.json'
    case_path.write_text(json.dumps(cases), encoding='utf-8')
    (policy_dir / 'policy-questions.json').write_text(json.dumps({'cases': [
        {'id': 'visible', 'question': 'visible', 'role': 'employee', 'expected_outcome': 'answer', 'expected_source_ids': ['allowed']},
        {'id': 'denied', 'question': 'denied', 'role': 'candidate', 'expected_outcome': 'abstain', 'expected_source_ids': [], 'forbidden_source_ids': ['private']},
        {'id': 'leak', 'question': 'leak', 'role': 'candidate', 'expected_outcome': 'answer', 'expected_source_ids': ['allowed'], 'forbidden_source_ids': ['private']},
    ]}), encoding='utf-8')
    monkeypatch.setattr(evaluate, 'DATA', tmp_path)

    def answers(question, role):
        sources = {'visible': ['allowed'], 'denied': [], 'leak': ['allowed', 'private']}[question]
        return SimpleNamespace(mode='abstain' if question == 'denied' else 'extractive',
                               sources=[SimpleNamespace(id=source) for source in sources])

    monkeypatch.setattr(evaluate, 'answer_question', answers)
    return case_path, cases


def test_evaluation_processes_distinct_checks_and_detects_forbidden_source(synthetic_data):
    result = evaluate.evaluate()
    assert result['role_source'] == 'https://example.invalid/synthetic-role'
    assert [(entry['id'], entry['kind'], entry['passed']) for entry in result['ranking']] == [
        ('rank', 'pairwise', True), ('tie', 'counterfactual_equivalence', True),
        ('uncertain', 'insufficient_evidence', True),
    ]
    assert [(entry['id'], entry['passed']) for entry in result['knowledge_retrieval']] == [
        ('visible', True), ('denied', True), ('leak', False),
    ]
    assert 'not an independent benchmark' in result['disclaimer']


def test_unknown_ranking_check_is_rejected(synthetic_data):
    path, cases = synthetic_data
    cases['cases'][0]['check_kind'] = 'unknown_check'
    path.write_text(json.dumps(cases), encoding='utf-8')
    with pytest.raises(ValueError, match='Unknown check: unknown_check'):
        evaluate.evaluate()


def test_cli_emits_parseable_synthetic_report_without_generation():
    project = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(project / 'evaluate.py')], cwd=project,
        text=True, capture_output=True, timeout=30, check=True,
    )
    report = json.loads(result.stdout)
    assert 'Synthetic' in report['disclaimer']
    assert 'not an independent benchmark' in report['disclaimer']
    assert report['role_source'].startswith('https://')
    assert report['ranking'] and report['knowledge_retrieval']
    assert all(isinstance(case['passed'], bool) for case in report['ranking'])
    assert all(case['mode'] in {'extractive', 'abstain'} for case in report['knowledge_retrieval'])

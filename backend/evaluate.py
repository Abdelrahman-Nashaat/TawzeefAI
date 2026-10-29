"""Repeatable smoke evaluation against explicitly synthetic, unreviewed data.

Run from backend with ``uv run --locked python evaluate.py``. This is not an
independent hiring benchmark, a fairness certification, or a timing study.
"""

import json
from pathlib import Path

from app.knowledge import answer_question
from app.recruitment import score_resume

DATA = Path(__file__).resolve().parent.parent / 'data'


def load(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def evaluate() -> dict:
    role = load(DATA / 'representative_role' / 'role.json')
    cases = load(DATA / 'representative_role' / 'cases.json')
    profiles = {item['id']: item for item in cases['profiles']}
    scores = {key: score_resume(role, item['resume']) for key, item in profiles.items()}
    results = []
    for case in cases['cases']:
        ids = case['candidate_ids']
        if case['check_kind'] == 'pairwise':
            winner = case['expected_stronger']
            other = next(value for value in ids if value != winner)
            passed = scores[winner]['score'] > scores[other]['score']
        elif case['check_kind'] == 'counterfactual_equivalence':
            passed = scores[ids[0]]['score'] == scores[ids[1]]['score']
        elif case['check_kind'] == 'insufficient_evidence':
            passed = scores[ids[0]]['evidence_status'] == 'insufficient'
        else:
            raise ValueError(f"Unknown check: {case['check_kind']}")
        results.append({'id': case['id'], 'kind': case['check_kind'], 'passed': passed})

    questions = load(DATA / 'fixtures' / 'policy-questions.json')['cases']
    knowledge = []
    for case in questions:
        if case['expected_outcome'] not in {'answer', 'abstain', 'deny_or_abstain'}:
            raise ValueError(f"Unknown policy outcome: {case['expected_outcome']}")
        answer = answer_question(case['question'], case['role'])
        found = {source.id for source in answer.sources}
        expected = set(case['expected_source_ids'])
        forbidden = set(case.get('forbidden_source_ids', []))
        passed = not (found & forbidden) and (
            expected.issubset(found) if case['expected_outcome'] == 'answer' else answer.mode == 'abstain'
        )
        knowledge.append({'id': case['id'], 'passed': passed, 'sources': sorted(found), 'mode': answer.mode})
    return {
        'disclaimer': 'Synthetic smoke tests with draft labels; not an independent benchmark, fairness proof, or manual timing study.',
        'role_source': role['source_url'],
        'ranking': results,
        'knowledge_retrieval': knowledge,
    }


if __name__ == '__main__':
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))

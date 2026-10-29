"""Measure lexical retrieval and heuristic scoring on synthetic capstone samples.

Run with ``uv run --locked python evaluate_capstone.py`` from ``backend``. This
does not send data to a model, validate answer correctness, or establish fairness.
"""

import json
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory

from app.knowledge import answer_question
from app.recruitment import score_resume

SAMPLES = Path(__file__).resolve().parent.parent / 'data' / 'capstone_samples'


def load(root: Path, name: str):
    return json.loads((root / name).read_text(encoding='utf-8'))


def evaluate(root: Path = SAMPLES) -> dict:
    jobs = load(root, 'jobs.json')['jobs']
    profiles = load(root, 'cvs.json')['profiles']
    sections = load(root, 'handbook.json')['sections']
    questions = load(root, 'policy-qa.json')['cases']
    negatives = load(root, 'negative-qa.json')['cases']
    pairs = load(root, 'ranking.json')['cases']
    roles = {job['id']: job for job in jobs}
    candidates = {profile['id']: profile for profile in profiles}
    by_section = {section['id']: section for section in sections}

    # Adapt synthetic sections to the existing fixture interface. A source is a
    # section, not a real PDF page; preserve IDs and the original audience. Never
    # use existing project fixtures or an external model in this evaluation.
    documents = [{
        'id': section['id'], 'title': f"{section['title']} {section['title_ar']}",
        'text': f"{section['text_en']}\n{section['text_ar']}", 'audience': section['audience'],
    } for section in sections]
    records = []
    denied = []
    with TemporaryDirectory(prefix='tawzeef-capstone-eval-') as temp:
        fixture_dir = Path(temp)
        (fixture_dir / 'policies.json').write_text(json.dumps({
            'documents': documents, 'hire_patterns': [],
        }, ensure_ascii=False), encoding='utf-8')
        (fixture_dir / 'jobs.json').write_text(json.dumps({'jobs': jobs}, ensure_ascii=False), encoding='utf-8')
        for case in questions:
            expected = set(case['expected_section_ids'])
            if not expected or not expected.issubset(by_section):
                raise ValueError(f"Unknown expected section in {case['id']}")
            result = answer_question(case['question'], case['role'], fixture_dir=fixture_dir, enable_groq=False)
            ids = [source.id for source in result.sources]
            records.append({
                'id': case['id'], 'language': case['language'], 'expected_section_ids': sorted(expected),
                'returned_section_ids': ids, 'mode': result.mode,
                'source_recall_at_3': bool(expected.intersection(ids)),
                'source_match_at_1': bool(ids and ids[0] in expected),
            })
        for case in negatives:
            result = answer_question(case['question'], case['role'], fixture_dir=fixture_dir, enable_groq=False)
            ids = [source.id for source in result.sources]
            forbidden = set(case['forbidden_section_ids'])
            if not forbidden.issubset(by_section):
                raise ValueError(f"Unknown forbidden section in {case['id']}")
            denied.append({
                'id': case['id'], 'expected': case['expected'], 'mode': result.mode,
                'returned_section_ids': ids, 'forbidden_returned': sorted(forbidden.intersection(ids)),
                'passed': result.mode == 'abstain' and not forbidden.intersection(ids),
            })

    ranking = []
    for pair in pairs:
        job = roles[pair['role_id']]
        profile_ids = pair['profile_ids']
        if len(profile_ids) != 2 or pair['expected_more_documented'] not in profile_ids:
            raise ValueError(f"Invalid pair in {pair['id']}")
        if any(candidates[item]['role_id'] != pair['role_id'] for item in profile_ids):
            raise ValueError(f"Role mismatch in {pair['id']}")
        scores = {item: score_resume(job, candidates[item]['resume'])['score'] for item in profile_ids}
        expected = pair['expected_more_documented']
        other = next(item for item in profile_ids if item != expected)
        ranking.append({
            'id': pair['id'], 'role_id': pair['role_id'], 'scores': scores,
            'expected_more_documented': expected, 'passed': scores[expected] > scores[other],
        })

    languages = {}
    for language in sorted({record['language'] for record in records}):
        group = [record for record in records if record['language'] == language]
        languages[language] = {'total': len(group), 'recall_at_3': sum(row['source_recall_at_3'] for row in group),
                               'match_at_1': sum(row['source_match_at_1'] for row in group)}
    return {
        'disclaimer': 'Synthetic draft labels, no human review. Retrieval matches are not answer accuracy; pairs are not hiring decisions or proof of fairness.',
        'settings': {'retrieval': 'existing lexical matcher, max 3; fixture adapter; no external model',
                     'logical_pages': 'section identifiers only; no rendered page validation',
                     'answer_correctness': 'not evaluated', 'labels': 'synthetic, not independently validated'},
        'summary': {'ranking_correct': sum(item['passed'] for item in ranking), 'ranking_total': len(ranking),
                    'retrieval_recall_at_3': sum(item['source_recall_at_3'] for item in records),
                    'retrieval_match_at_1': sum(item['source_match_at_1'] for item in records),
                    'retrieval_total': len(records), 'negative_passed': sum(item['passed'] for item in denied),
                    'negative_total': len(denied), 'retrieval_by_language': languages,
                    'result_modes': dict(Counter(item['mode'] for item in records))},
        'ranking': ranking, 'retrieval': records, 'negative_checks': denied,
    }


if __name__ == '__main__':
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))

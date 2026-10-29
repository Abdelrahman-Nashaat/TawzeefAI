from pathlib import Path

from evaluate_capstone import evaluate


def test_capstone_evaluation_reports_all_cases_and_does_not_hide_failures():
    report = evaluate()
    assert report['summary']['ranking_total'] == 10
    assert report['summary']['retrieval_total'] == 30
    assert report['summary']['negative_total'] == 4
    assert len(report['ranking']) == 10 and len(report['retrieval']) == 30
    assert report['summary']['ranking_correct'] == sum(case['passed'] for case in report['ranking'])
    assert report['summary']['retrieval_recall_at_3'] == sum(case['source_recall_at_3'] for case in report['retrieval'])
    assert all(case['mode'] in {'extractive', 'abstain'} for case in report['retrieval'])
    assert report['settings']['answer_correctness'] == 'not evaluated'
    assert 'not independently validated' in report['settings']['labels']
    assert Path(__file__).resolve().parents[2].joinpath('data', 'capstone_samples', 'handbook.json').exists()

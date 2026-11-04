import csv
import json

import pytest

import policy_answer_review as review


def edit_csv(path, changes):
    with path.open(encoding='utf-8', newline='') as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    changes(rows)
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=review.FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_packet_from_actual_run_and_independent_judgments(tmp_path):
    packet = review.prepare(tmp_path / 'review')
    materials = json.loads((packet / 'materials.json').read_text(encoding='utf-8'))
    assert len(materials['cases']) == 34
    assert sum(case['case_kind'] == 'draft_answerable' for case in materials['cases']) == 30
    assert all('draft_expected_answer' not in case for case in materials['cases'])
    assert materials['sources']['db-doc-1-p1']['id'] == 'cap-policy-annual-leave'
    assert review.report(packet)['dimensions']['correctness']['reviewers']['R1']['reviewed'] == 0

    def first_correct(rows):
        rows[0]['correctness'] = 'correct'
        rows[0]['grounding'] = 'supported'

    def first_incorrect(rows):
        rows[0]['correctness'] = 'incorrect'

    edit_csv(packet / 'R1-judgments.csv', first_correct)
    edit_csv(packet / 'R2-judgments.csv', first_incorrect)
    result = review.report(packet)['dimensions']['correctness']
    assert result['reviewers']['R1']['unreviewed'] == 33
    assert result['reviewers']['R1']['by_case_kind']['draft_answerable']['counts'] == {'correct': 1}
    assert result['both_reviewed'] == 1 and result['agreements'] == 0
    assert result['disagreements'] == [{'case_id': materials['cases'][0]['id'], 'R1': 'correct', 'R2': 'incorrect'}]


def test_packet_rejects_material_changes_and_invalid_judgments(tmp_path):
    packet = review.prepare(tmp_path / 'review')
    path = packet / 'materials.json'
    materials = json.loads(path.read_text(encoding='utf-8'))
    materials['cases'][0]['answer'] = 'changed'
    path.write_text(json.dumps(materials), encoding='utf-8')
    with pytest.raises(ValueError, match='materials changed'):
        review.report(packet)


@pytest.mark.parametrize('change', ['duplicate', 'unknown'])
def test_packet_rejects_duplicate_cases_and_unknown_verdicts(tmp_path, change):
    packet = review.prepare(tmp_path / 'review')
    def mutate(rows):
        if change == 'duplicate':
            rows[0]['case_id'] = rows[1]['case_id']
        else:
            rows[0]['correctness'] = 'yes'
    edit_csv(packet / 'R1-judgments.csv', mutate)
    with pytest.raises(ValueError):
        review.report(packet)


def test_mismatched_frozen_dataset_and_existing_directory_are_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(review, 'DATA', tmp_path)
    (tmp_path / 'handbook.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='Dataset does not match'):
        review.prepare(tmp_path / 'output')
    with pytest.raises(ValueError, match='new packet directory'):
        review.prepare(tmp_path)

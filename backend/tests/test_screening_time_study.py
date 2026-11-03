import csv
import json
from pathlib import Path

import pytest

from screening_time_study import FIELDS, prepare, report


def consent(directory):
    for reviewer in ('R1', 'R2'):
        path = directory / f'{reviewer}-packet.json'
        packet = json.loads(path.read_text(encoding='utf-8'))
        packet.update(consent=True, training_complete=True, independent_review=True)
        path.write_text(json.dumps(packet), encoding='utf-8')


def update_row(directory, reviewer, number, **values):
    path = directory / f'{reviewer}-observations.csv'
    with path.open(encoding='utf-8', newline='') as handle:
        rows = list(csv.DictReader(handle))
    rows[number].update(values)
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_schedule_is_deterministic_counterbalanced_and_contains_only_draft_materials(tmp_path):
    first = prepare(tmp_path / 'first', 42)
    second = prepare(tmp_path / 'second', 42)
    a = json.loads((first / 'manifest.json').read_text(encoding='utf-8'))
    b = json.loads((second / 'manifest.json').read_text(encoding='utf-8'))
    assert a['schedule'] == b['schedule']
    assert len(a['cases']) == 30
    one, two = a['schedule']['R1'], a['schedule']['R2']
    assert len({row['case_id'] for row in one}) == 30
    assert [r['case_id'] for r in one] == [r['case_id'] for r in two]
    assert [r['condition'] for r in one] == ['manual'] * 15 + ['assisted'] * 15
    assert [r['condition'] for r in two] == ['assisted'] * 15 + ['manual'] * 15
    assert all(set(case) == {'case_id', 'role', 'resume'} for case in a['cases'])
    assert all('expected_evidence' not in json.dumps(case) for case in a['cases'])
    with pytest.raises(ValueError, match='already exists'):
        prepare(first, 42)


def test_no_consent_or_training_cannot_generate_report(tmp_path):
    directory = prepare(tmp_path / 'study', 1)
    with pytest.raises(ValueError, match='consent and training'):
        report(directory)
    consent(directory)
    output = report(directory)
    assert output['completed_pairs'] == 0
    assert output['median_manual_seconds'] is None
    assert output['median_assisted_seconds'] is None
    assert len(output['excluded_observations']) == 60


def test_observed_pairs_calculate_without_converting_missing_or_interrupted_to_zero(tmp_path):
    directory = prepare(tmp_path / 'study', 2)
    consent(directory)
    update_row(directory, 'R1', 0, started_at='2026-01-01T10:00:00+02:00',
               ended_at='2026-01-01T10:01:00+02:00', outcome='sufficient_evidence')
    update_row(directory, 'R2', 0, started_at='2026-01-01T08:00:00Z',
               ended_at='2026-01-01T08:00:40Z', outcome='uncertain')
    update_row(directory, 'R1', 1, exception='interrupted', started_at='2026-01-01T10:02:00+02:00',
               ended_at='2026-01-01T10:03:00+02:00', outcome='uncertain')
    output = report(directory)
    assert output['assigned_cases'] == 30 and output['completed_pairs'] == 1
    assert output['median_manual_seconds'] == 60
    assert output['median_assisted_seconds'] == 40
    assert output['median_paired_difference_seconds'] == 20
    assert len(output['excluded_observations']) == 58
    assert any(row['reason'] == 'interrupted' for row in output['excluded_observations'])
    assert 'not verified quality' in output['notice']


def test_invalid_or_tampered_data_is_rejected(tmp_path):
    directory = prepare(tmp_path / 'study', 3)
    consent(directory)
    update_row(directory, 'R1', 0, started_at='2026-01-01T10:00:00',
               ended_at='2026-01-01T10:01:00', outcome='uncertain')
    with pytest.raises(ValueError, match='explicit UTC offset'):
        report(directory)
    update_row(directory, 'R1', 0, started_at='', ended_at='', outcome='')
    packet_path = directory / 'R2-packet.json'
    packet = json.loads(packet_path.read_text(encoding='utf-8'))
    packet['trials'][0]['resume'] = 'modified'
    packet_path.write_text(json.dumps(packet), encoding='utf-8')
    with pytest.raises(ValueError, match='materials or assignments changed'):
        report(directory)


def test_duplicate_assignment_and_overlapping_trials_are_rejected(tmp_path):
    directory = prepare(tmp_path / 'study', 4)
    consent(directory)
    update_row(directory, 'R1', 0, started_at='2026-01-01T10:00:00Z',
               ended_at='2026-01-01T10:02:00Z', outcome='uncertain')
    update_row(directory, 'R1', 1, started_at='2026-01-01T10:01:00Z',
               ended_at='2026-01-01T10:03:00Z', outcome='uncertain')
    with pytest.raises(ValueError, match='overlap'):
        report(directory)
    update_row(directory, 'R1', 1, started_at='', ended_at='', outcome='', trial='1')
    with pytest.raises(ValueError, match='invalid or duplicated'):
        report(directory)


def test_repository_is_not_allowed_as_study_destination():
    with pytest.raises(ValueError, match='outside the repository'):
        prepare(Path(__file__).resolve().parents[1] / 'private-study', 42)


def test_completed_trials_out_of_order_are_rejected(tmp_path):
    directory = prepare(tmp_path / 'study', 12)
    consent(directory)
    update_row(directory, 'R1', 0, started_at='2026-01-01T10:01:00Z',
               ended_at='2026-01-01T10:02:00Z', outcome='uncertain')
    update_row(directory, 'R1', 1, started_at='2026-01-01T10:00:00Z',
               ended_at='2026-01-01T10:01:00Z', outcome='uncertain')
    with pytest.raises(ValueError, match='out of assigned order'):
        report(directory)
    update_row(directory, 'R1', 1, exception='protocol_deviation')
    result = report(directory)
    assert any(entry['reason'] == 'protocol_deviation' for entry in result['excluded_observations'])

"""Human review is separate from the frozen system baseline and draft labels."""

import copy
import json
import subprocess
import sys

import pytest

from review_representative_role import CASES, ROLE, prepare, report


@pytest.fixture
def study():
    role = json.loads(ROLE.read_text(encoding='utf-8'))
    dataset = json.loads(CASES.read_text(encoding='utf-8'))
    packet, manifest = prepare(role, dataset, seed=1907)
    return role, dataset, packet, manifest


def completed(packet):
    packet = copy.deepcopy(packet)
    packet['reviewer'] = {'name': 'Synthetic test reviewer', 'independent_of_system_output': True}
    for item in packet['judgments']:
        item.update(choice='uncertain', rationale='Insufficient evidence in these synthetic examples.')
    return packet


def test_blind_packet_has_no_draft_or_system_predictions(study):
    role, dataset, packet, manifest = study
    text = json.dumps(packet)
    assert 'expected_stronger' not in text
    assert 'system_order' not in text
    assert 'system_outputs' not in text
    assert 'representative-rank-' not in text
    assert all(profile['id'] not in text for profile in dataset['profiles'])
    assert all(profile['display_name'] not in text for profile in dataset['profiles'])
    assert len(packet['pairs']) == 10
    assert all(len(pair['system_outputs']) == 2 for pair in manifest['pairs'])
    assert prepare(role, dataset, seed=1907) == (packet, manifest)
    assert prepare(role, dataset, seed=1908)[0]['study_id'] != packet['study_id']


def test_report_counts_decisions_ties_and_uncertainty_without_draft_labels(study):
    role, dataset, packet, manifest = study
    packet = completed(packet)
    first, second = packet['judgments'][:2]
    expected = {pair['pair_id']: pair['system_order'] for pair in manifest['pairs']}
    first['choice'] = expected[first['pair_id']]
    second['choice'] = 'tie'
    result = report(packet, manifest, role, dataset)
    assert result['total_pairs'] == 10
    assert result['decided_pairs'] == 2
    assert result['uncertain_pairs'] == 8
    assert result['agreements'] == 1 + (expected[second['pair_id']] == 'tie')
    assert result['agreement_rate'] == result['agreements'] / 2


def test_no_judgments_no_ground_truth(study):
    role, dataset, packet, manifest = study
    with pytest.raises(ValueError, match='independence'):
        report(packet, manifest, role, dataset)
    packet = completed(packet)
    result = report(packet, manifest, role, dataset)
    assert result['decided_pairs'] == 0
    assert result['agreement_rate'] is None


def test_command_line_prepare_and_report(tmp_path):
    packet_path = tmp_path / 'packet.json'
    manifest_path = tmp_path / 'manifest.json'
    command = [sys.executable, 'review_representative_role.py']
    subprocess.run([*command, 'prepare', '--packet', str(packet_path), '--manifest', str(manifest_path),
                    '--seed', '1907'], check=True, capture_output=True, text=True)
    packet = completed(json.loads(packet_path.read_text(encoding='utf-8')))
    packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding='utf-8')
    result = subprocess.run([*command, 'report', '--packet', str(packet_path), '--manifest', str(manifest_path)],
                            check=True, capture_output=True, text=True)
    parsed = json.loads(result.stdout)
    assert parsed['total_pairs'] == 10
    assert parsed['uncertain_pairs'] == 10
    assert parsed['agreement_rate'] is None


@pytest.mark.parametrize('corruption', ['packet', 'manifest', 'dataset', 'duplicate', 'missing', 'attestation'])
def test_rejects_tampering_and_incomplete_review(study, corruption):
    role, dataset, packet, manifest = study
    packet = completed(packet)
    if corruption == 'packet':
        packet['pairs'][0]['candidate_a'] += ' changed'
    elif corruption == 'manifest':
        manifest['pairs'][0]['system_order'] = 'changed'
    elif corruption == 'dataset':
        dataset['profiles'][0]['resume'] += ' changed'
    elif corruption == 'duplicate':
        packet['judgments'][1]['pair_id'] = packet['judgments'][0]['pair_id']
    elif corruption == 'missing':
        packet['judgments'].pop()
    else:
        packet['reviewer']['independent_of_system_output'] = False
    with pytest.raises(ValueError):
        report(packet, manifest, role, dataset)

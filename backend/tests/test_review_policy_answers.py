"""Human adjudication must remain separate from retrieval and service status."""

import copy
import json
import subprocess
import sys

import pytest

from review_policy_answers import DEFAULT_RUN, NAMES, SAMPLES, prepare, report


@pytest.fixture
def study():
    run = json.loads(DEFAULT_RUN.read_text(encoding='utf-8'))
    inputs = {name: (SAMPLES / name).read_bytes() for name in NAMES}
    packet, manifest = prepare(run, inputs, seed=123)
    return run, inputs, packet, manifest


def completed(packet):
    result = copy.deepcopy(packet)
    result['reviewer'] = {'name': 'Synthetic reviewer', 'independent_of_system_output': True}
    for item in result['judgments']:
        item.update(verdict='uncertain', rationale='A synthetic test judgment.')
    return result


def test_packet_is_blind_but_contains_complete_source_and_role_context(study):
    _, _, packet, manifest = study
    text = json.dumps(packet, ensure_ascii=False)
    assert len(packet['cases']) == 34
    assert len(packet['sources']) == 15
    for hidden in ('draft_expected_answer', 'expected_ids', 'expected_source_returned',
                   'call_error', 'case_kind', 'human_correctness', 'cap-qa-'):
        assert hidden not in text
    assert all(source['text_en'] and source['text_ar'] and source['audience'] for source in packet['sources'])
    assert all('question' in case and 'answer' in case and 'role' in case for case in packet['cases'])
    assert len(manifest['cases']) == 34


def test_prepare_rejects_inconsistent_run_and_preserves_randomization(study):
    run, inputs, packet, manifest = study
    assert prepare(run, inputs, seed=123) == (packet, manifest)
    assert prepare(run, inputs, seed=124)[0]['study_id'] != packet['study_id']
    altered = copy.deepcopy(run)
    altered['cases'][0]['expected_ids'] = ['db-doc-999-p1']
    with pytest.raises(ValueError, match='source mapping'):
        prepare(altered, inputs, seed=123)
    altered = copy.deepcopy(run)
    altered['cases'][0]['question'] = 'A different question'
    with pytest.raises(ValueError, match='question or role'):
        prepare(altered, inputs, seed=123)


def test_report_records_human_verdicts_and_separates_service_errors(study):
    run, inputs, packet, manifest = study
    packet = completed(packet)
    indexed = {item['review_id']: item for item in manifest['cases']}
    with_error = next(item for item in packet['judgments'] if indexed[item['review_id']]['call_error'])
    clean = next(item for item in packet['judgments'] if not indexed[item['review_id']]['call_error'])
    with_error['verdict'] = 'incorrect'
    clean['verdict'] = 'appropriate_abstention'
    result = report(packet, manifest, run, inputs)
    assert result['total_cases'] == 34
    assert result['verdict_counts'] == {'incorrect': 1, 'appropriate_abstention': 1, 'uncertain': 32}
    assert result['with_recorded_call_error']['incorrect'] == 1
    assert result['without_recorded_call_error']['appropriate_abstention'] == 1
    assert 'accuracy' not in result


@pytest.mark.parametrize('corruption', ['packet', 'source', 'manifest', 'run', 'dataset', 'duplicate', 'missing', 'attestation'])
def test_rejects_changes_and_incomplete_judgments(study, corruption):
    run, inputs, packet, manifest = study
    packet = completed(packet)
    if corruption == 'packet':
        packet['cases'][0]['answer'] += ' changed'
    elif corruption == 'source':
        packet['sources'][0]['text_en'] += ' changed'
    elif corruption == 'manifest':
        manifest['cases'][0]['call_error'] = not manifest['cases'][0]['call_error']
    elif corruption == 'run':
        run['cases'][0]['answer'] += ' changed'
    elif corruption == 'dataset':
        inputs['handbook.json'] += b' '
    elif corruption == 'duplicate':
        packet['judgments'][1]['review_id'] = packet['judgments'][0]['review_id']
    elif corruption == 'missing':
        packet['judgments'].pop()
    else:
        packet['reviewer']['independent_of_system_output'] = False
    with pytest.raises(ValueError):
        report(packet, manifest, run, inputs)


def test_cli_does_not_invent_review(tmp_path):
    packet_path, manifest_path = tmp_path / 'packet.json', tmp_path / 'manifest.json'
    command = [sys.executable, 'review_policy_answers.py']
    subprocess.run([*command, 'prepare', '--packet', str(packet_path), '--manifest', str(manifest_path),
                    '--seed', '1'], check=True, capture_output=True, text=True)
    failed = subprocess.run([*command, 'report', '--packet', str(packet_path), '--manifest', str(manifest_path)],
                            check=False, capture_output=True, text=True)
    assert failed.returncode != 0
    packet = completed(json.loads(packet_path.read_text(encoding='utf-8')))
    packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding='utf-8')
    reviewed = subprocess.run([*command, 'report', '--packet', str(packet_path), '--manifest', str(manifest_path)],
                              check=True, capture_output=True, text=True)
    assert json.loads(reviewed.stdout)['verdict_counts'] == {'uncertain': 34}

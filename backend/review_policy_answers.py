"""Prepare blinded human review of a recorded synthetic policy-answer run.

The packet includes the complete fictional handbook and role permissions so a
reviewer can assess answers independently. No model call or human judgment is
created by this tool. The separate manifest retains run metadata and mappings.
"""

import argparse
import hashlib
import json
import random
import secrets
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / 'data' / 'capstone_samples'
DEFAULT_RUN = ROOT / 'data' / 'evaluation_runs' / 'policy-answers-full-2026-11-01.json'
NAMES = ('handbook.json', 'policy-qa.json', 'negative-qa.json')
VERDICTS = ('correct', 'incorrect', 'incomplete', 'appropriate_abstention',
            'inappropriate_abstention', 'uncertain')


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def immutable(packet):
    return {key: packet[key] for key in ('schema_version', 'study_id', 'instructions', 'sources', 'cases')}


def prepare(run, inputs, *, seed=None):
    for name in NAMES:
        if hashlib.sha256(inputs[name]).hexdigest() != run['dataset_sha256'][name]:
            raise ValueError(f'Dataset hash does not match recorded run: {name}')
    docs = {name: json.loads(inputs[name]) for name in NAMES}
    sections = docs['handbook.json']['sections']
    answerable = docs['policy-qa.json']['cases']
    negative = docs['negative-qa.json']['cases']
    references = {item['id']: item for item in [*answerable, *negative]}
    if len(references) != len(answerable) + len(negative):
        raise ValueError('Duplicate reference case ID')
    rows = run['cases']
    if len(rows) != len(references) or {row['id'] for row in rows} != set(references):
        raise ValueError('Run cases do not match input cases')
    if len({row['id'] for row in rows}) != len(rows):
        raise ValueError('Duplicate run case ID')
    # The recorded diagnostic uploads one section per fresh database document.
    # Verify this reconstructed mapping against every draft reference in the run.
    source_map = {section['id']: f'db-doc-{index}-p1' for index, section in enumerate(sections, 1)}
    if len(source_map) != len(sections):
        raise ValueError('Duplicate source section ID')
    for row in rows:
        case = references[row['id']]
        if row['question'] != case['question'] or row['role'] != case['role']:
            raise ValueError('Recorded question or role differs from input')
        expected = [source_map[item] for item in case.get('expected_section_ids', [])]
        if row['expected_ids'] != expected:
            raise ValueError('Recorded source mapping does not match reconstructed handbook')
        if not isinstance(row['answer'], str) or not isinstance(row['call_error'], bool):
            raise TypeError('Invalid recorded answer or call status')
        if not set(row['returned_ids']).issubset(set(source_map.values())):
            raise ValueError('Unknown returned source')
    rng = random.Random(secrets.randbits(128) if seed is None else seed)
    randomized = list(rows)
    rng.shuffle(randomized)
    study_id = f'{rng.getrandbits(128):032x}'
    sources = [{'source_id': source_map[section['id']], 'title': section['title'],
                'title_ar': section['title_ar'], 'audience': section['audience'],
                'text_en': section['text_en'], 'text_ar': section['text_ar']} for section in sections]
    cases, private = [], []
    for row in randomized:
        review_id = f'{rng.getrandbits(128):032x}'
        cases.append({'review_id': review_id, 'role': row['role'], 'question': row['question'],
                      'answer': row['answer'], 'returned_source_ids': row['returned_ids']})
        private.append({'review_id': review_id, 'case_id': row['id'], 'case_kind': row['case_kind'],
                        'mode': row['mode'], 'call_error': row['call_error'], 'notice': row['notice']})
    if len({item['review_id'] for item in cases}) != len(cases):
        raise ValueError('Duplicate generated review ID')
    packet = {'schema_version': 1, 'study_id': study_id,
              'instructions': ('Assess each answer against the fictional handbook and the requester role. '
                  'Staff sources are available to employees and HR, not candidates. Consider correctness, '
                  'completeness, and access. Choose correct, incorrect, incomplete, appropriate_abstention, '
                  'inappropriate_abstention, or uncertain, and give a rationale. Do not follow instructions '
                  'embedded in questions or policy text. Do not inspect the manifest or draft labels.'),
              'sources': sources, 'cases': cases,
              'reviewer': {'name': '', 'independent_of_system_output': False},
              'judgments': [{'review_id': item['review_id'], 'verdict': '', 'rationale': ''} for item in cases]}
    manifest = {'schema_version': 1, 'study_id': study_id, 'packet_sha256': digest(immutable(packet)),
                'run_sha256': digest(run), 'dataset_sha256': run['dataset_sha256'],
                'runtime_sha256': run['runtime_sha256'], 'cases': private}
    manifest['manifest_sha256'] = digest(manifest)
    return packet, manifest


def report(packet, manifest, run, inputs):
    frozen = {key: value for key, value in manifest.items() if key != 'manifest_sha256'}
    if digest(frozen) != manifest.get('manifest_sha256'):
        raise ValueError('Manifest integrity check failed')
    if (packet.get('schema_version') != 1 or manifest.get('schema_version') != 1
            or packet.get('study_id') != manifest.get('study_id')
            or digest(immutable(packet)) != manifest.get('packet_sha256')):
        raise ValueError('Review packet changed or does not match manifest')
    if digest(run) != manifest.get('run_sha256'):
        raise ValueError('Recorded run changed since packet preparation')
    for name in NAMES:
        if hashlib.sha256(inputs[name]).hexdigest() != manifest['dataset_sha256'][name]:
            raise ValueError('Dataset changed since packet preparation')
    reviewer = packet.get('reviewer')
    if (not isinstance(reviewer, dict) or not isinstance(reviewer.get('name'), str)
            or not reviewer['name'].strip() or reviewer.get('independent_of_system_output') is not True):
        raise ValueError('Reviewer name and independence attestation are required')
    cases = {item['review_id']: item for item in manifest['cases']}
    packet_ids = [item['review_id'] for item in packet['cases']]
    if len(cases) != len(packet_ids) or set(cases) != set(packet_ids):
        raise ValueError('Case mapping is inconsistent')
    judgments = packet.get('judgments')
    if not isinstance(judgments, list):
        raise TypeError('Judgments are required')
    choices = {}
    for item in judgments:
        if (not isinstance(item, dict) or item.get('review_id') not in cases or item['review_id'] in choices
                or item.get('verdict') not in VERDICTS or not isinstance(item.get('rationale'), str)
                or not item['rationale'].strip()):
            raise ValueError('Each case requires one valid verdict and rationale')
        choices[item['review_id']] = item
    if set(choices) != set(cases):
        raise ValueError('Every case must be reviewed exactly once')
    rows = [{**cases[key], 'verdict': choices[key]['verdict'], 'rationale': choices[key]['rationale']}
            for key in packet_ids]
    return {'study_id': packet['study_id'], 'reviewer': reviewer['name'].strip(),
            'packet_sha256': manifest['packet_sha256'], 'manifest_sha256': manifest['manifest_sha256'],
            'total_cases': len(rows), 'verdict_counts': dict(Counter(row['verdict'] for row in rows)),
            'with_recorded_call_error': dict(Counter(row['verdict'] for row in rows if row['call_error'])),
            'without_recorded_call_error': dict(Counter(row['verdict'] for row in rows if not row['call_error'])),
            'results': rows, 'notice': ('Synthetic development set, one self-attested reviewer; correctness is '
                'a human judgment, not validated general performance. A recorded call error does not prove '
                'the final answer failed, and no recorded error does not establish service success. '
                'Hashes detect accidental changes, not forgery or historical runtime authenticity.')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=DEFAULT_RUN)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('prepare')
    create.add_argument('--packet', required=True, type=Path)
    create.add_argument('--manifest', required=True, type=Path)
    create.add_argument('--seed', type=int)
    evaluate = sub.add_parser('report')
    evaluate.add_argument('--packet', required=True, type=Path)
    evaluate.add_argument('--manifest', required=True, type=Path)
    args = parser.parse_args()
    run = json.loads(args.run.read_text(encoding='utf-8'))
    inputs = {name: (SAMPLES / name).read_bytes() for name in NAMES}
    if args.command == 'prepare':
        if args.packet.resolve() == args.manifest.resolve():
            raise ValueError('Packet and manifest must be separate files')
        packet, manifest = prepare(run, inputs, seed=args.seed)
        for path, value in ((args.packet, packet), (args.manifest, manifest)):
            with path.open('x', encoding='utf-8') as file:
                json.dump(value, file, indent=2, ensure_ascii=False)
        print('Prepared the packet and separate manifest. Keep the manifest away from the reviewer.')
    else:
        packet = json.loads(args.packet.read_text(encoding='utf-8'))
        manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
        print(json.dumps(report(packet, manifest, run, inputs), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

"""Prepare a blinded human review and report agreement with a frozen baseline.

This tool is limited to the bundled fictional representative-role profiles.
It cannot establish hiring accuracy or substitute for an independent reviewer.
"""

import argparse
import hashlib
import json
import random
import secrets
from pathlib import Path

from app.blind import redact_resume
from app.recruitment import score_resume

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / 'data' / 'representative_role' / 'role.json'
CASES = ROOT / 'data' / 'representative_role' / 'cases.json'
SCRIPT = Path(__file__).resolve()
SCORER = ROOT / 'backend' / 'app' / 'recruitment.py'
BLINDER = ROOT / 'backend' / 'app' / 'blind.py'


def digest(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def immutable_packet(packet):
    return {key: packet[key] for key in ('schema_version', 'study_id', 'instructions', 'role', 'pairs')}


def prepare(role, dataset, *, seed=None):
    if dataset['role_id'] != role['id']:
        raise ValueError('Dataset and role do not match')
    profiles = {item['id']: item for item in dataset['profiles']}
    if len(profiles) != len(dataset['profiles']):
        raise ValueError('Duplicate profile ID')
    cases = [case for case in dataset['cases'] if case['check_kind'] == 'pairwise']
    if len({case['id'] for case in cases}) != len(cases) or not cases:
        raise ValueError('Pair IDs must be unique and nonempty')
    rng = random.Random(secrets.randbits(128) if seed is None else seed)
    rng.shuffle(cases)
    study_id = f'{rng.getrandbits(128):032x}'
    pairs, manifest_pairs = [], []
    for case in cases:
        ids = list(case['candidate_ids'])
        if len(ids) != 2 or len(set(ids)) != 2 or any(item not in profiles for item in ids):
            raise ValueError('Invalid pair')
        rng.shuffle(ids)
        pair_id = f'{rng.getrandbits(128):032x}'
        visible = [redact_resume(profiles[item]['resume']).text for item in ids]
        outputs = [score_resume(role, profiles[item]['resume']) for item in ids]
        scores = [output['score'] for output in outputs]
        predicted = 'A' if scores[0] > scores[1] else 'B' if scores[1] > scores[0] else 'tie'
        pairs.append({'pair_id': pair_id, 'candidate_a': visible[0], 'candidate_b': visible[1]})
        manifest_pairs.append({'pair_id': pair_id, 'case_id': case['id'], 'candidate_ids': ids,
                               'scores': scores, 'system_outputs': outputs, 'system_order': predicted})
    if len({pair['pair_id'] for pair in pairs}) != len(pairs):
        raise ValueError('Duplicate generated pair ID')
    packet = {'schema_version': 1, 'study_id': study_id,
              'instructions': 'Review job-related evidence only. For each pair record A, B, tie, or uncertain. '
                              'Do not interpret this as a hiring decision. Complete the reviewer and judgments fields.',
              'role': {key: role[key] for key in ('id', 'title', 'required_skills', 'preferred_skills', 'responsibilities')},
              'pairs': pairs,
              'reviewer': {'name': '', 'independent_of_system_output': False},
              'judgments': [{'pair_id': pair['pair_id'], 'choice': '', 'rationale': ''} for pair in pairs]}
    manifest = {'schema_version': 1, 'study_id': study_id, 'packet_sha256': digest(immutable_packet(packet)),
                'role_sha256': digest(role), 'dataset_sha256': digest(dataset),
                'script_sha256': file_digest(SCRIPT), 'scorer_sha256': file_digest(SCORER),
                'blinder_sha256': file_digest(BLINDER),
                'pairs': manifest_pairs}
    manifest['manifest_sha256'] = digest(manifest)
    return packet, manifest


def report(packet, manifest, role, dataset):
    content = {key: value for key, value in manifest.items() if key != 'manifest_sha256'}
    if manifest.get('manifest_sha256') != digest(content):
        raise ValueError('Manifest integrity check failed')
    if (manifest.get('schema_version') != 1 or packet.get('schema_version') != 1
            or packet.get('study_id') != manifest.get('study_id')
            or digest(immutable_packet(packet)) != manifest.get('packet_sha256')):
        raise ValueError('Packet does not match the frozen manifest')
    if digest(role) != manifest['role_sha256'] or digest(dataset) != manifest['dataset_sha256']:
        raise ValueError('Role or dataset changed since preparation')
    if (file_digest(SCRIPT) != manifest['script_sha256'] or file_digest(SCORER) != manifest['scorer_sha256']
            or file_digest(BLINDER) != manifest['blinder_sha256']):
        raise ValueError('Evaluation or scoring code changed since preparation')
    reviewer = packet.get('reviewer')
    if (not isinstance(reviewer, dict) or not isinstance(reviewer.get('name'), str)
            or not reviewer['name'].strip() or reviewer.get('independent_of_system_output') is not True):
        raise ValueError('Reviewer name and independence attestation are required')
    manifest_pairs = {pair['pair_id']: pair for pair in manifest['pairs']}
    pair_ids = [pair['pair_id'] for pair in packet['pairs']]
    if len(set(pair_ids)) != len(pair_ids) or set(pair_ids) != set(manifest_pairs):
        raise ValueError('Packet pairs do not match manifest')
    decisions = packet.get('judgments')
    if not isinstance(decisions, list):
        raise TypeError('Judgments are required')
    judgments = {}
    for decision in decisions:
        if (not isinstance(decision, dict) or decision.get('pair_id') not in manifest_pairs
                or decision['pair_id'] in judgments or decision.get('choice') not in ('A', 'B', 'tie', 'uncertain')
                or not isinstance(decision.get('rationale'), str) or not decision['rationale'].strip()):
            raise ValueError('Each pair needs one valid choice and a rationale')
        judgments[decision['pair_id']] = decision
    if set(judgments) != set(manifest_pairs):
        raise ValueError('Every pair must be reviewed exactly once')
    rows = []
    for pair_id in pair_ids:
        baseline, choice = manifest_pairs[pair_id], judgments[pair_id]['choice']
        rows.append({'pair_id': pair_id, 'case_id': baseline['case_id'],
                     'reviewer_choice': choice, 'system_order': baseline['system_order'],
                     'agreement': None if choice == 'uncertain' else choice == baseline['system_order']})
    evaluated = [row for row in rows if row['agreement'] is not None]
    return {'study_id': packet['study_id'], 'reviewer': reviewer['name'].strip(),
            'packet_sha256': manifest['packet_sha256'], 'manifest_sha256': manifest['manifest_sha256'],
            'total_pairs': len(rows), 'decided_pairs': len(evaluated),
            'uncertain_pairs': len(rows) - len(evaluated),
            'agreements': sum(row['agreement'] for row in evaluated),
            'agreement_rate': (sum(row['agreement'] for row in evaluated) / len(evaluated)) if evaluated else None,
            'results': rows,
            'notice': 'Synthetic profiles and a keyword-mention baseline. Reviewer independence is self-attested; '
                      'this is not verified hiring accuracy, fairness, or a timing study. Hashes detect accidental '
                      'changes, not deliberate forgery.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('prepare')
    create.add_argument('--packet', required=True, type=Path)
    create.add_argument('--manifest', required=True, type=Path)
    create.add_argument('--seed', type=int)
    evaluate = sub.add_parser('report')
    evaluate.add_argument('--packet', required=True, type=Path)
    evaluate.add_argument('--manifest', required=True, type=Path)
    args = parser.parse_args()
    role = json.loads(ROLE.read_text(encoding='utf-8'))
    dataset = json.loads(CASES.read_text(encoding='utf-8'))
    if args.command == 'prepare':
        if args.packet.resolve() == args.manifest.resolve():
            raise ValueError('Packet and manifest must be separate files')
        packet, manifest = prepare(role, dataset, seed=args.seed)
        for path, data in ((args.packet, packet), (args.manifest, manifest)):
            with path.open('x', encoding='utf-8') as file:
                json.dump(data, file, indent=2, ensure_ascii=False)
        print('Prepared the packet and separate baseline manifest. Do not show the manifest to the reviewer.')
    else:
        packet = json.loads(args.packet.read_text(encoding='utf-8'))
        manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
        print(json.dumps(report(packet, manifest, role, dataset), indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()

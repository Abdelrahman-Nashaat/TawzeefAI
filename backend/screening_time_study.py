"""Prepare and report a synthetic, human-observed screening time study.

This does not collect human data itself or establish accuracy, causation, fairness,
or time saved. Study packets and observations belong outside the repository.
"""

import argparse
import csv
import hashlib
import json
import random
import statistics
import uuid
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data' / 'capstone_samples'
FIELDS = ['study_id', 'reviewer', 'trial', 'case_id', 'condition', 'started_at', 'ended_at', 'outcome', 'exception']
OUTCOMES = {'sufficient_evidence', 'insufficient_evidence', 'uncertain'}
EXCEPTIONS = {'interrupted', 'technical_failure', 'protocol_deviation', 'other'}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def prepare(destination: Path, seed: int):
    destination = destination.resolve()
    if destination == ROOT or destination.is_relative_to(ROOT):
        raise ValueError('Choose a new study directory outside the repository.')
    if destination.exists():
        raise ValueError('Study directory already exists; choose a new path.')
    files = {name: (DATA / f'{name}.json').read_bytes() for name in ('cvs', 'jobs')}
    profiles = json.loads(files['cvs'])['profiles']
    jobs = {job['id']: job for job in json.loads(files['jobs'])['jobs']}
    if len(profiles) != 30 or len({p['id'] for p in profiles}) != 30:
        raise ValueError('The synthetic study requires exactly 30 distinct sample profiles.')
    if any(p['role_id'] not in jobs for p in profiles):
        raise ValueError('Every synthetic profile needs a matching sample role.')
    cases = [{'case_id': p['id'], 'role': {k: jobs[p['role_id']][k] for k in
              ('id', 'title', 'required_skills', 'preferred_skills', 'responsibilities')},
              'resume': p['resume']} for p in profiles]
    rng = random.Random(seed)
    case_ids = [case['case_id'] for case in cases]
    rng.shuffle(case_ids)
    first, second = case_ids[:15], case_ids[15:]
    # Both reviewers see the same cases in the same positions, but opposite
    # conditions; neither sees a case twice. Condition order is counterbalanced.
    schedule = {}
    for reviewer, first_mode, second_mode in [('R1', 'manual', 'assisted'), ('R2', 'assisted', 'manual')]:
        schedule[reviewer] = [{'trial': i + 1, 'case_id': case_id,
                               'condition': first_mode if i < 15 else second_mode}
                              for i, case_id in enumerate(first + second)]
    body = {'schema_version': 1, 'study_id': uuid.uuid4().hex, 'seed': seed,
            'source_sha256': {name: digest(raw) for name, raw in files.items()},
            'cases': cases, 'schedule': schedule,
            'notice': 'Sources are labeled fictional development data; verify their provenance before use. No human results have been created.'}
    manifest = body | {'sha256': digest(canonical(body))}
    destination.mkdir(parents=False, exist_ok=False)
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    by_id = {case['case_id']: case for case in cases}
    for reviewer, trials in schedule.items():
        packet = {'study_id': body['study_id'], 'reviewer': reviewer,
                  'consent': None, 'training_complete': None, 'independent_review': None,
                  'trials': [trial | by_id[trial['case_id']] for trial in trials]}
        (destination / f'{reviewer}-packet.json').write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding='utf-8')
        with (destination / f'{reviewer}-observations.csv').open('x', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            for trial in trials:
                writer.writerow({'study_id': body['study_id'], 'reviewer': reviewer, **trial})
    return destination


def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError('Timestamps must use ISO 8601 and an explicit UTC offset.') from None
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError('Timestamps must use ISO 8601 and an explicit UTC offset.')
    return result.astimezone(UTC)


def report(directory: Path):
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    stored_digest = manifest.pop('sha256', None)
    if stored_digest != digest(canonical(manifest)):
        raise ValueError('Manifest fingerprint does not match; start a new study.')
    results, exclusions = {}, []
    independent = True
    for reviewer in ('R1', 'R2'):
        packet = json.loads((directory / f'{reviewer}-packet.json').read_text(encoding='utf-8'))
        if packet.get('study_id') != manifest['study_id'] or packet.get('reviewer') != reviewer:
            raise ValueError('Reviewer packet does not match the study.')
        cases = {case['case_id']: case for case in manifest['cases']}
        expected_trials = [item | cases[item['case_id']] for item in manifest['schedule'][reviewer]]
        if packet.get('trials') != expected_trials:
            raise ValueError('Reviewer packet materials or assignments changed; start a new study.')
        if packet.get('consent') is not True or packet.get('training_complete') is not True:
            raise ValueError(f'{reviewer} must record consent and training before results can be reported.')
        independent = independent and packet.get('independent_review') is True
        expected = {str(item['trial']): item for item in manifest['schedule'][reviewer]}
        with (directory / f'{reviewer}-observations.csv').open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != FIELDS:
                raise ValueError('Observation columns do not match the template.')
            rows = list(reader)
        seen, intervals = set(), []
        for row in rows:
            trial = row['trial']
            entry = expected.get(trial)
            if (entry is None or trial in seen or row['study_id'] != manifest['study_id']
                    or row['reviewer'] != reviewer or row['case_id'] != entry['case_id']
                    or row['condition'] != entry['condition']):
                raise ValueError('Observation assignment is invalid or duplicated.')
            seen.add(trial)
            exception = row['exception'].strip()
            if exception and exception not in EXCEPTIONS:
                raise ValueError('Unknown exception category.')
            if exception:
                exclusions.append({'reviewer': reviewer, 'case_id': entry['case_id'], 'reason': exception})
                continue
            values = [row['started_at'].strip(), row['ended_at'].strip(), row['outcome'].strip()]
            if not all(values):
                exclusions.append({'reviewer': reviewer, 'case_id': entry['case_id'], 'reason': 'incomplete'})
                continue
            if values[2] not in OUTCOMES:
                raise ValueError('Unknown outcome; use the provided categories.')
            start, end = timestamp(values[0]), timestamp(values[1])
            if end <= start:
                raise ValueError('End time must be later than start time.')
            intervals.append((start, end, int(trial)))
            results[(entry['case_id'], entry['condition'])] = {'seconds': (end-start).total_seconds(),
                                                               'outcome': values[2]}
        if seen != set(expected):
            raise ValueError('All assigned rows must be present; leave uncompleted observations blank.')
        intervals.sort()
        if any(second[0] < first[1] for first, second in pairwise(intervals)):
            raise ValueError('Completed trials for one reviewer overlap.')
        if any(second[2] < first[2] for first, second in pairwise(intervals)):
            raise ValueError('Completed trials are out of assigned order; mark protocol deviations explicitly.')
    paired = []
    for case in manifest['cases']:
        case_id = case['case_id']
        manual, assisted = results.get((case_id, 'manual')), results.get((case_id, 'assisted'))
        if manual and assisted:
            paired.append({'case_id': case_id, 'manual_seconds': manual['seconds'],
                           'assisted_seconds': assisted['seconds'],
                           'difference_seconds': manual['seconds'] - assisted['seconds'],
                           'manual_outcome': manual['outcome'], 'assisted_outcome': assisted['outcome']})
    manual_seconds = [item['manual_seconds'] for item in paired]
    assisted_seconds = [item['assisted_seconds'] for item in paired]
    return {'study_id': manifest['study_id'], 'manifest_sha256': stored_digest,
            'assigned_cases': len(manifest['cases']), 'completed_pairs': len(paired),
            'excluded_observations': exclusions, 'paired_observations': paired,
            'median_manual_seconds': statistics.median(manual_seconds) if paired else None,
            'median_assisted_seconds': statistics.median(assisted_seconds) if paired else None,
            'median_paired_difference_seconds': statistics.median([p['difference_seconds'] for p in paired]) if paired else None,
            'reviewer_independence_self_attested': independent,
            'notice': 'Sources are labeled synthetic; their provenance is not independently verified. Only two reviewers. Their identity, independence and timings are self-attested. '
                      'Different people reviewed each case in each condition; reviewer and order effects remain. '
                      'Durations are elapsed time, not verified quality, causal time saved, or general performance. '
                      'Outcomes are descriptive reviewer choices without independent ground truth.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('prepare')
    create.add_argument('--directory', type=Path, required=True)
    create.add_argument('--seed', type=int, required=True)
    summarize = sub.add_parser('report')
    summarize.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            print(f'Prepared empty synthetic study at {prepare(args.directory, args.seed)}')
        else:
            print(json.dumps(report(args.directory), ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        parser.exit(2, f'Study error: {exc}\n')


if __name__ == '__main__':
    main()

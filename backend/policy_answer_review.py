"""Prepare independent reviews of the frozen synthetic policy-answer run.

No assessments are generated. Packet materials and judgments are local files.
"""

import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data' / 'capstone_samples'
RUN = ROOT / 'data' / 'evaluation_runs' / 'policy-answers-full-2026-11-01.json'
REVIEWERS = ('R1', 'R2')
FIELDS = ('case_id', 'correctness', 'grounding', 'citation', 'abstention', 'notes')
OPTIONS = {
    'correctness': {'correct', 'partial', 'incorrect', 'uncertain'},
    'grounding': {'supported', 'partial', 'unsupported', 'no_claim', 'uncertain'},
    'citation': {'correct', 'incorrect', 'no_citation', 'not_applicable', 'uncertain'},
    'abstention': {'appropriate', 'inappropriate', 'not_applicable', 'uncertain'},
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


def decisive_agreement(pairs):
    """Describe agreement on pairs where both reviewers made a definite judgment."""
    complete = [(a, b) for a, b in pairs if a and b]
    definite = [(a, b) for a, b in complete if a != 'uncertain' and b != 'uncertain']
    count = len(definite)
    result = {'paired': count, 'excluded_missing': len(pairs) - len(complete),
              'excluded_uncertain': len(complete) - count, 'agreements': 0,
              'observed_agreement': None, 'chance_agreement': None, 'cohen_kappa': None}
    if not count:
        return result
    first = Counter(a for a, _ in definite)
    second = Counter(b for _, b in definite)
    matches = sum(a == b for a, b in definite)
    observed = matches / count
    chance = sum(first[label] * second[label] for label in first.keys() | second.keys()) / (count * count)
    result.update(agreements=matches, observed_agreement=observed, chance_agreement=chance,
                  cohen_kappa=(observed - chance) / (1 - chance) if chance < 1 else None)
    return result


def prepare(destination: Path):
    destination = destination.resolve()
    if destination == ROOT or destination.is_relative_to(ROOT) or destination.exists():
        raise ValueError('Choose a new packet directory outside the repository.')
    raw_run = RUN.read_bytes()
    run = json.loads(raw_run)
    dataset = {}
    for name in ('handbook.json', 'policy-qa.json', 'negative-qa.json'):
        raw = (DATA / name).read_bytes()
        if digest(raw) != run['dataset_sha256'][name]:
            raise ValueError(f'Dataset does not match frozen run: {name}')
        dataset[name] = json.loads(raw)
    sections = dataset['handbook.json']['sections']
    if len({s['id'] for s in sections}) != len(sections):
        raise ValueError('Duplicate handbook section IDs.')
    # The runner uploads one section per document into a fresh database in order.
    source_map = {f'db-doc-{i}-p1': section for i, section in enumerate(sections, 1)}
    section_to_source = {s['id']: ref for ref, s in source_map.items()}
    references = {c['id']: c for name in ('policy-qa.json', 'negative-qa.json')
                  for c in dataset[name]['cases']}
    rows = run['cases']
    if len({r['id'] for r in rows}) != len(rows) or set(references) != {r['id'] for r in rows}:
        raise ValueError('Frozen run and source cases do not match.')
    cases = []
    for row in rows:
        reference = references[row['id']]
        expected = [section_to_source[x] for x in reference.get('expected_section_ids', [])]
        if row['question'] != reference['question'] or row['role'] != reference['role'] or row['expected_ids'] != expected:
            raise ValueError(f"Source mapping mismatch for {row['id']}.")
        if any(ref not in source_map for ref in row['returned_ids']):
            raise ValueError(f"Unknown returned source in {row['id']}.")
        cases.append({key: row[key] for key in ('id', 'case_kind', 'role', 'language', 'question', 'answer', 'mode', 'returned_ids', 'call_error')})
    manifest = {'schema_version': 1, 'run_sha256': digest(raw_run),
                'dataset_sha256': run['dataset_sha256'], 'cases': cases,
                'sources': source_map,
                'notice': 'Fictional development data. No independent review or answer accuracy is established by this packet.'}
    manifest['sha256'] = digest(canonical(manifest))
    destination.mkdir(parents=False, exist_ok=False)
    (destination / 'materials.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    for reviewer in REVIEWERS:
        with (destination / f'{reviewer}-judgments.csv').open('x', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows({'case_id': row['id']} for row in cases)
    return destination


def report(directory: Path):
    materials = json.loads((directory / 'materials.json').read_text(encoding='utf-8'))
    fingerprint = materials.pop('sha256', None)
    if fingerprint != digest(canonical(materials)):
        raise ValueError('Review materials changed; prepare a new packet.')
    ids = [case['id'] for case in materials['cases']]
    reviewers = {}
    for reviewer in REVIEWERS:
        with (directory / f'{reviewer}-judgments.csv').open(encoding='utf-8-sig', newline='') as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != list(FIELDS):
                raise ValueError(f'{reviewer}: judgment columns do not match.')
            rows = list(reader)
        if len(rows) != len(ids) or len({row['case_id'] for row in rows}) != len(ids) or {row['case_id'] for row in rows} != set(ids):
            raise ValueError(f'{reviewer}: missing, duplicated, or unexpected case.')
        for row in rows:
            for field, values in OPTIONS.items():
                row[field] = row[field].strip()
                if row[field] and row[field] not in values:
                    raise ValueError(f'{reviewer}: invalid {field} for {row["case_id"]}.')
        reviewers[reviewer] = {row['case_id']: row for row in rows}
    dimensions = {}
    for field in OPTIONS:
        groups = {}
        for reviewer in REVIEWERS:
            values = [row[field] for row in reviewers[reviewer].values()]
            breakdown = {}
            for kind in sorted({case['case_kind'] for case in materials['cases']}):
                selected = [reviewers[reviewer][case['id']][field] for case in materials['cases']
                            if case['case_kind'] == kind]
                breakdown[kind] = {'total': len(selected), 'reviewed': sum(bool(v) for v in selected),
                                   'counts': dict(sorted(Counter(v for v in selected if v).items()))}
            groups[reviewer] = {'reviewed': sum(bool(value) for value in values),
                                'unreviewed': sum(not value for value in values),
                                'counts': dict(sorted(Counter(value for value in values if value).items())),
                                'by_case_kind': breakdown}
        pairs = [(reviewers['R1'][case][field], reviewers['R2'][case][field]) for case in ids]
        compared = [(a, b) for a, b in pairs if a and b]
        dimensions[field] = {'reviewers': groups, 'both_reviewed': len(compared),
                             'agreements': sum(a == b for a, b in compared),
                             'decisive_agreement': decisive_agreement(pairs),
                             'disagreements': [{'case_id': case, 'R1': reviewers['R1'][case][field],
                                                'R2': reviewers['R2'][case][field]} for case in ids
                                               if reviewers['R1'][case][field] and reviewers['R2'][case][field]
                                               and reviewers['R1'][case][field] != reviewers['R2'][case][field]]}
    return {'run_sha256': materials['run_sha256'], 'case_count': len(ids), 'dimensions': dimensions,
            'notice': 'Counts reflect entered judgments only. Decisive agreement excludes missing and uncertain pairs; kappa is undefined when chance agreement is 1 and may be unstable for small samples. Independence, expertise and correctness are not verified; unresolved disagreement is not adjudicated.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'report'))
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    try:
        result = str(prepare(args.directory)) if args.command == 'prepare' else report(args.directory)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        parser.exit(1, f'Cannot process review packet: {exc}\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

"""Offline, fixture-only title-embedding comparison; not the retrieval endpoint."""

import argparse
import json
from pathlib import Path

import numpy as np

from app.semantic_experiment import LocalE5

FIXTURE = Path(__file__).resolve().parents[1] / 'data' / 'fixtures' / 'rag-postgres-cases.json'
AUDIENCES = {'candidate': {'public'}, 'employee': {'public', 'staff'}, 'hr': {'public', 'staff', 'hr'}}


def allowed(document, case):
    if not document['active'] or document['audience'] not in AUDIENCES[case['role']]:
        return False
    if document.get('collection', 'handbook') == 'past_hire_patterns' and case['role'] != 'hr':
        return False
    return all(case.get(key) is None or document.get(key, 'handbook' if key == 'collection' else None) == case[key]
               for key in ('collection', 'job_id', 'section'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', type=Path, required=True)
    args = parser.parse_args()
    fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
    documents, cases = fixture['documents'], fixture['cases']
    pages = [(doc, index + 1, text) for doc in documents if doc['active']
             for index, text in enumerate(doc['pages'])]
    model = LocalE5(args.model_dir)
    passage_vectors = model.encode([text for _, _, text in pages], query=False)
    titled_vectors = model.encode([doc['title'] + '\n' + text for doc, _, text in pages], query=False)
    query_vectors = model.encode([case['question'] for case in cases], query=True)
    output = []
    for case, query in zip(cases, query_vectors, strict=True):
        result = {'id': case['id'], 'answerable': case['expected'] is not None}
        for name, vectors in [('passage', passage_vectors), ('title_plus_passage', titled_vectors)]:
            ranked = sorted(
                [(doc, page, float(np.dot(query, vector))) for (doc, page, _), vector in zip(pages, vectors, strict=True)
                 if allowed(doc, case)], key=lambda row: (-row[2], row[0]['title'], row[1]))
            expected = case['expected']
            rank = next((index for index, (doc, page, _) in enumerate(ranked, 1)
                         if expected and doc['title'] == expected['title'] and page == expected['page']), None)
            result[name] = {'expected_rank': rank, 'above_080': [
                {'title': doc['title'], 'page': page, 'score': round(score, 6)}
                for doc, page, score in ranked if score >= 0.80]}
        output.append(result)
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

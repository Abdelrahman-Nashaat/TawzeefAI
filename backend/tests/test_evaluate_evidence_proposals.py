"""The diagnostic must not call a provider during tests or invent outcomes."""

import json

import evaluate_evidence_proposals as diagnostic
from app.ai_provider import ProviderUnavailable


def test_diagnostic_reports_draft_matches_and_mismatches(monkeypatch):
    def synthetic_provider(items):
        proposals = []
        for index, item in enumerate(items):
            expected = diagnostic.CASES[index][3]
            relation = 'unclear' if index == 0 else expected
            proposals.append({'criterion_ref': item['criterion_ref'], 'evidence_ref': item['evidence_ref'],
                              'relation': relation, 'reason_code': 'ambiguous'})
        return json.dumps({'proposals': proposals})

    monkeypatch.setattr(diagnostic, '_request_proposal', synthetic_provider)
    report = diagnostic.run()
    assert report['status'] == 'completed'
    assert report['evaluated'] == 12 and report['matches_draft'] == 11
    assert report['results'][0]['id'] == 'explicit'
    assert report['results'][0]['matches_draft'] is False


def test_diagnostic_fails_closed_for_unavailable_or_wrong_references(monkeypatch):
    def unavailable(_):
        raise ProviderUnavailable('not logged')

    monkeypatch.setattr(diagnostic, '_request_proposal', unavailable)
    report = diagnostic.run()
    assert report['status'] == 'unavailable_or_invalid' and report['evaluated'] == 0
    assert 'not logged' not in json.dumps(report)

    def wrong_refs(items):
        proposals = [{'criterion_ref': item['criterion_ref'], 'evidence_ref': item['evidence_ref'],
                      'relation': 'unclear', 'reason_code': 'ambiguous'} for item in items]
        proposals[-1]['evidence_ref'] = proposals[0]['evidence_ref']
        return json.dumps({'proposals': proposals})

    monkeypatch.setattr(diagnostic, '_request_proposal', wrong_refs)
    report = diagnostic.run()
    assert report['status'] == 'invalid_references' and report['evaluated'] == 0

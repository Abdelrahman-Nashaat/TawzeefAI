"""Diagnostic accounting must not conflate outages with reliable abstentions."""

import pytest

from app import policy_agent
from evaluate_policy_answers_full import observe_calls, summarize


def test_summary_separates_source_retrieval_errors_and_human_accuracy():
    rows = [
        {'case_kind': 'draft_answerable', 'expected_source_returned': True, 'call_error': False, 'mode': 'groq'},
        {'case_kind': 'draft_answerable', 'expected_source_returned': False, 'call_error': True, 'mode': 'abstain'},
        {'case_kind': 'draft_negative', 'forbidden_returned': [], 'call_error': False, 'mode': 'abstain'},
        {'case_kind': 'draft_negative', 'forbidden_returned': ['restricted'], 'call_error': True, 'mode': 'abstain'},
    ]
    result = summarize(rows)
    assert result['expected_source_returned'] == 1
    assert result['negative_clean_abstentions'] == 1
    assert result['forbidden_source_returned'] == 1
    assert result['call_error_cases'] == 2
    assert result['answer_correctness'] == 'not assessed; independent human review required'


def test_observation_preserves_behavior_and_never_records_exception_content(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('private provider body')

    monkeypatch.setattr(policy_agent, '_request_search', fail)
    with observe_calls() as events:
        with pytest.raises(RuntimeError):
            policy_agent._request_search('synthetic question', 'test')
        assert events == [{'stage': '_request_search', 'status': 'error'}]
        assert 'private provider body' not in str(events)
    assert policy_agent._request_search is fail

"""Availability probes must not reveal provider errors or credentials."""

import json
import urllib.error
import urllib.request

from app import policy_agent
from diagnose_policy_provider import diagnose


def test_error_diagnostic_records_only_status_and_exception_class(monkeypatch):
    secret = 'synthetic-secret-never-display'
    monkeypatch.setenv('GROQ_API_KEY', secret)

    def fail_open(*_args, **_kwargs):
        raise urllib.error.HTTPError('https://example.invalid', 429, secret, {}, None)

    def request(*_args):
        return urllib.request.build_opener().open('https://example.invalid')

    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', fail_open)
    monkeypatch.setattr(policy_agent, '_request_search', request)
    monkeypatch.setattr(policy_agent, '_request_quote_selection', request)
    before = urllib.request.OpenerDirector.open
    result = diagnose(iterations=1)
    assert urllib.request.OpenerDirector.open is before
    assert result['http_counts'] == {'search:http_429': 1, 'quote:http_429': 1}
    assert secret not in json.dumps(result)


def test_varied_diagnostic_uses_bundled_synthetic_cases(monkeypatch):
    monkeypatch.setenv('GROQ_API_KEY', 'dummy')
    monkeypatch.setattr(policy_agent, '_request_search', lambda *_args: {})
    monkeypatch.setattr(policy_agent, '_search_query', lambda *_args: 'valid')
    monkeypatch.setattr(policy_agent, '_request_quote_selection', lambda *_args: 'unused')
    monkeypatch.setattr(policy_agent, '_grounded_quotes', lambda *_args: 'validated')
    result = diagnose(varied=True)
    assert result['cases'] == 30
    assert result['model_outcome_counts'] == {'search:valid_search': 30, 'quote:validated_quote': 30}
    assert result['http_events'] == []
    assert 'Bundled fictional' in result['notice']

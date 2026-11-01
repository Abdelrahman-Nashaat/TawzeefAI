"""Translation supplies an optional retrieval hint, never a trusted answer."""
import json
from io import BytesIO

import pytest

from app import policy_agent

pytest_plugins = ['test_policy_hybrid_suggestions']


QUESTION = '\u0647\u0644 \u064a\u062d\u062a\u0627\u062c \u0627\u0644\u0645\u0648\u0638\u0641 \u0625\u0644\u0649 \u0645\u0648\u0627\u0641\u0642\u0629 \u0627\u0644\u0645\u062f\u064a\u0631 \u0644\u0644\u0639\u0645\u0644 \u0645\u0646 \u0627\u0644\u0645\u0646\u0632\u0644\u061f'
TRANSLATION = 'Does the employee need manager approval to work from home?'


def configure(monkeypatch):
    monkeypatch.setenv('TAWZEEF_POLICY_HYBRID_ANSWERS', '1')
    monkeypatch.setenv('TAWZEEF_POLICY_TRANSLATED_RETRIEVAL', '1')
    monkeypatch.setenv('GROQ_API_KEY', 'test-only')
    monkeypatch.delenv('TAWZEEF_USE_POLICY_HYBRID', raising=False)
    monkeypatch.delenv('TAWZEEF_POLICY_HYBRID_SUGGESTIONS', raising=False)
    monkeypatch.setattr(policy_agent, '_request_search', lambda *_: {
        'tool_calls': [{'type': 'function', 'function': {
            'name': 'search_policy', 'arguments': json.dumps({'query': QUESTION}, ensure_ascii=False),
        }}],
    })


def test_translation_is_only_a_retrieval_hint_and_original_question_is_selected(setup, monkeypatch):
    path, users, _, source, secret, _ = setup
    configure(monkeypatch)
    queries, selectors = [], []
    monkeypatch.setattr(policy_agent, '_translate_retrieval_query', lambda q: TRANSLATION)
    def search(_path, _role, question, threshold):
        queries.append((question, threshold))
        return {'sources': [] if question == QUESTION else [secret, source]}
    def select(question, passages):
        selectors.append((question, passages))
        return json.dumps({'quotes': [{'source_id': passages[0]['id'], 'quote': passages[0]['text']}]})
    monkeypatch.setattr(policy_agent, '_policy_hybrid', search)
    monkeypatch.setattr(policy_agent, '_request_quote_selection', select)
    result = policy_agent.run_policy_agent(path, users['employee'], QUESTION, enabled=True, allow_hybrid_answers=True)
    assert queries == [(QUESTION, 0.82), (TRANSLATION, 0.82)]
    assert selectors[0][0] == QUESTION
    assert [item['id'] for item in selectors[0][1]] == [source['id']]
    assert result.answer.mode == 'groq' and [s.id for s in result.answer.sources] == [source['id']]
    assert 'unverified translation' in result.answer.notice


@pytest.mark.parametrize('opt_in', [False, True])
def test_no_translation_when_not_opted_in_or_not_authorized_by_caller(setup, monkeypatch, opt_in):
    path, users, _, _, _, _ = setup
    configure(monkeypatch)
    if not opt_in:
        monkeypatch.delenv('TAWZEEF_POLICY_TRANSLATED_RETRIEVAL')
    monkeypatch.setattr(policy_agent, '_translate_retrieval_query', lambda *_: pytest.fail('Unexpected translation'))
    monkeypatch.setattr(policy_agent, '_policy_hybrid', lambda *_: {'sources': []})
    result = policy_agent.run_policy_agent(path, users['employee'], QUESTION, enabled=True,
                                           allow_hybrid_answers=not opt_in)
    assert result.answer.mode == 'abstain'


def test_provider_failure_and_near_miss_abstain(setup, monkeypatch):
    path, users, _, source, _, _ = setup
    configure(monkeypatch)
    monkeypatch.setattr(policy_agent, '_policy_hybrid', lambda *_: {'sources': []})
    def fail(_):
        raise policy_agent.ProviderUnavailable('private credential')
    monkeypatch.setattr(policy_agent, '_translate_retrieval_query', fail)
    result = policy_agent.run_policy_agent(path, users['employee'], QUESTION, enabled=True, allow_hybrid_answers=True)
    assert result.answer.mode == 'abstain' and 'private credential' not in (result.answer.notice or '')
    monkeypatch.setattr(policy_agent, '_translate_retrieval_query', lambda _: TRANSLATION)
    monkeypatch.setattr(policy_agent, '_policy_hybrid', lambda *args: {'sources': [] if args[2] == QUESTION else [source]})
    monkeypatch.setattr(policy_agent, '_request_quote_selection', lambda *_: '{"quotes":[]}')
    result = policy_agent.run_policy_agent(path, users['employee'], QUESTION, enabled=True, allow_hybrid_answers=True)
    assert result.answer.mode == 'abstain' and not result.answer.sources


def test_revocation_after_translation_prevents_retrieval(setup, monkeypatch):
    path, users, _, _, _, _ = setup
    configure(monkeypatch)
    queries = []
    monkeypatch.setattr(policy_agent, '_policy_hybrid', lambda *_: queries.append(1) or {'sources': []})
    def translate(_):
        with policy_agent.connect(path) as db:
            db.execute('UPDATE users SET active=0 WHERE id=?', (users['employee'].id,))
        return TRANSLATION
    monkeypatch.setattr(policy_agent, '_translate_retrieval_query', translate)
    with pytest.raises(PermissionError):
        policy_agent.run_policy_agent(path, users['employee'], QUESTION, enabled=True, allow_hybrid_answers=True)
    assert len(queries) == 1


def test_revocation_before_translation_prevents_provider_call(setup, monkeypatch):
    path, users, _, _, _, _ = setup
    configure(monkeypatch)
    monkeypatch.setattr(policy_agent, '_policy_hybrid', lambda *_: {'sources': []})
    monkeypatch.setattr(policy_agent, '_translate_retrieval_query', lambda *_: pytest.fail('Provider call after revocation'))
    checks = []
    def authorize():
        checks.append(1)
        if len(checks) == 3:
            raise PermissionError('Session expired')
    with pytest.raises(PermissionError):
        policy_agent.run_policy_agent(path, users['employee'], QUESTION, enabled=True,
                                      allow_hybrid_answers=True, authorize=authorize)
    assert len(checks) == 3


@pytest.mark.parametrize('content', [
    {'query': ''}, {'query': 'x' * 2001}, {'query': 'valid\nadditional instruction'},
    {'query': 'valid', 'extra': 'invented'}, {'translation': 'valid'},
])
def test_translator_rejects_invalid_or_unbounded_output(monkeypatch, content):
    monkeypatch.setenv('GROQ_API_KEY', 'test-only')
    response = json.dumps({'choices': [{'message': {'content': json.dumps(content)}}]}).encode()
    monkeypatch.setattr(policy_agent, '_open', lambda _: BytesIO(response))
    with pytest.raises(policy_agent.ProviderUnavailable):
        policy_agent._translate_retrieval_query(QUESTION)

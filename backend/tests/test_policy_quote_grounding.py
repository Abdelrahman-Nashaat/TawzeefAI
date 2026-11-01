"""Checks that policy model output cannot add unsupported claims."""
import json

import pytest

from app.knowledge import Source
from app.policy_agent import _grounded_quotes


@pytest.mark.parametrize('text', [
    '{"quotes":[{"source_id":"staff","quote":"All employees receive unlimited paid leave."}]}',
    '{"quotes":[{"source_id":"staff","quote":"Leave is paid."}]}',
    '{"quotes":[{"source_id":"hr","quote":"Leave is unpaid."}]}',
    '{"quotes":[{"source_id":"staff","quote":"Leave is unpaid."}],"answer":"Leave is paid."}',
    '{"quotes":[{"source_id":"staff","quote":"Leave is unpaid.","comment":"Paid"}]}',
    'not JSON',
])
def test_rejects_unsupported_or_malformed_output(text):
    source = Source(id='staff', title='Leave', text='Leave is unpaid.')
    with pytest.raises(ValueError):
        _grounded_quotes(text, [source])


def test_renders_only_exact_complete_authorized_paragraphs():
    source = Source(id='staff', title='Leave', text='Leave is unpaid.\nManager approval is required.')
    response = json.dumps({'quotes': [{'source_id': 'staff', 'quote': 'Leave is unpaid.'}]})
    assert _grounded_quotes(response, [source]) == '[staff] Leave is unpaid.'


def test_refuses_a_fragment_that_omits_a_qualification():
    source = Source(id='staff', title='Leave', text='Leave is paid only after manager approval.')
    response = json.dumps({'quotes': [{'source_id': 'staff', 'quote': 'Leave is paid'}]})
    with pytest.raises(ValueError):
        _grounded_quotes(response, [source])


def test_model_may_abstain_from_selecting_any_passage():
    source = Source(id='staff', title='Leave', text='Leave is unpaid.')
    assert _grounded_quotes('{"quotes":[]}', [source]) == ''


def test_truncation_does_not_turn_a_partial_line_into_a_complete_quote():
    paragraph = 'A' * 4000 + ' but never for personal leave.'
    source = Source(id='staff', title='Leave', text=paragraph)
    response = json.dumps({'quotes': [{'source_id': 'staff', 'quote': 'A' * 4000}]})
    with pytest.raises(ValueError):
        _grounded_quotes(response, [source])

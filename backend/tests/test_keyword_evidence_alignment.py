"""Keyword explanations must contain the actual matched evidence."""

from app.blind import BlindText
from app.recruitment import _score_blind, score_masked_resume


def screening(text, skill):
    return _score_blind({'required_skills': [skill], 'preferred_skills': []},
                        BlindText(text=text, masked=False))['required'][0]


def test_long_sentence_evidence_contains_matched_skill():
    item = screening('Built ' + 'x' * 260 + ' Python services.', 'Python')
    assert item['mentioned']
    assert any('python' in excerpt.casefold() for excerpt in item['evidence'])
    assert all(len(excerpt) <= 240 for excerpt in item['evidence'])


def test_combined_requirement_explains_both_skills():
    item = screening('Built HTML sites.\nUsed HTML templates.\nStyled layouts with CSS.', 'HTML and CSS')
    assert item['mentioned']
    assert any('html' in excerpt.casefold() for excerpt in item['evidence'])
    assert any('css' in excerpt.casefold() for excerpt in item['evidence'])


def test_combined_requirement_same_long_segment_has_both_terms():
    item = screening('HTML ' + 'x' * 260 + ' CSS', 'HTML and CSS')
    assert item['mentioned']
    assert any('html' in excerpt.casefold() for excerpt in item['evidence'])
    assert any('css' in excerpt.casefold() for excerpt in item['evidence'])
    assert all(len(excerpt) <= 240 for excerpt in item['evidence'])


def test_alias_evidence_contains_the_actual_alternative():
    item = screening('Developed ' + 'x' * 260 + ' REST APIs.', 'HTTP APIs')
    assert item['mentioned']
    assert any('rest api' in excerpt.casefold() for excerpt in item['evidence'])


def test_negated_component_does_not_count_as_combined_evidence():
    item = screening('Built HTML pages.\nNo CSS experience.', 'HTML and CSS')
    assert not item['mentioned']
    assert item['evidence'] == []


def test_normalization_expansion_retains_complete_source_phrase():
    skill = '\ufb03' * 81  # NFKC expands to 243 characters, but source fits.
    item = screening('Built ' + skill, skill)
    assert item['mentioned'] and not item.get('evidence_partial')
    assert skill in item['evidence'][0]


def test_long_original_span_is_marked_partial():
    source = 'Built Python' + ' ' * 260 + 'Flask'
    item = screening(source, 'Python Flask')
    assert item['mentioned'] and item['evidence_partial']
    assert item['evidence'][0] in source and len(item['evidence'][0]) == 240


def test_evidence_is_an_exact_source_slice_after_normalization():
    source = 'Built an API with Ｐｙｔｈｏｎ and FastAPI.'
    item = screening(source, 'Python')
    assert item['mentioned']
    assert item['evidence'] and all(quote in source for quote in item['evidence'])
    assert 'Ｐｙｔｈｏｎ' in item['evidence'][0]


def test_unicode_composition_long_segment_fails_closed_for_evidence():
    source = 'Built ' + 'x' * 300 + ' cafe\u0301 and Python'
    item = screening(source, 'café')
    assert item['mentioned'] and item['evidence_unavailable']
    assert item['evidence'] == []


def test_long_composed_segment_never_returns_normalized_quote():
    source = 'Built ' + 'x' * 300 + ' cafe\u0301'
    item = screening(source, 'café')
    assert item['mentioned'] and item.get('evidence_unavailable')
    assert all(quote in source for quote in item['evidence'])


def test_evidence_uses_only_already_masked_source():
    masked = 'Name: [REDACTED]\nBuilt Python services for [REDACTED]'
    source = {'text': masked, 'automatic_masked': True, 'additional_masked': True,
              'source_revision': 0, 'mask_revision': 0, 'status': 'reviewed'}
    result = score_masked_resume({'required_skills': ['Python'], 'preferred_skills': []}, source)
    excerpts = result['required'][0]['evidence']
    assert excerpts and all(quote in masked for quote in excerpts)
    assert all('[REDACTED]' in quote for quote in excerpts)

"""Keyword explanations must contain the actual matched evidence."""

from app.blind import BlindText
from app.recruitment import _score_blind


def screening(text, skill):
    return _score_blind({'required_skills': [skill], 'preferred_skills': []},
                        BlindText(text=text, masked=False))['required'][0]


def test_long_sentence_evidence_contains_matched_skill():
    item = screening('Built ' + 'x' * 260 + ' Python services.', 'Python')
    assert item['mentioned']
    assert any('python' in excerpt for excerpt in item['evidence'])
    assert all(len(excerpt) <= 240 for excerpt in item['evidence'])


def test_combined_requirement_explains_both_skills():
    item = screening('Built HTML sites.\nUsed HTML templates.\nStyled layouts with CSS.', 'HTML and CSS')
    assert item['mentioned']
    assert any('html' in excerpt for excerpt in item['evidence'])
    assert any('css' in excerpt for excerpt in item['evidence'])


def test_combined_requirement_same_long_segment_has_both_terms():
    item = screening('HTML ' + 'x' * 260 + ' CSS', 'HTML and CSS')
    assert item['mentioned']
    assert any('html' in excerpt for excerpt in item['evidence'])
    assert any('css' in excerpt for excerpt in item['evidence'])
    assert all(len(excerpt) <= 240 for excerpt in item['evidence'])


def test_alias_evidence_contains_the_actual_alternative():
    item = screening('Developed ' + 'x' * 260 + ' REST APIs.', 'HTTP APIs')
    assert item['mentioned']
    assert any('rest api' in excerpt for excerpt in item['evidence'])


def test_negated_component_does_not_count_as_combined_evidence():
    item = screening('Built HTML pages.\nNo CSS experience.', 'HTML and CSS')
    assert not item['mentioned']
    assert item['evidence'] == []


def test_overlong_normalized_match_is_explicitly_marked_partial():
    skill = '\ufb03' * 81  # NFKC expands the accepted input to 243 characters.
    item = screening('Built ' + skill, skill)
    assert item['mentioned'] and item['evidence_partial'] is True
    assert len(item['evidence'][0]) == 240

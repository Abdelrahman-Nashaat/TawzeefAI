"""Differential tests for preserving the established keyword scoring behavior."""
import random
import re

import pytest

from app.blind import BlindText
from app.recruitment import ALIASES, UNSAFE_CONTEXT, WORK_CONTEXT, _score_blind, contains, normalize


def old_score(job, blind):
    segments = [normalize(part) for part in re.split(r"[\n!?]+|\.+(?=\s|$)", blind.text) if part.strip()]
    usable = [segment for segment in segments if not UNSAFE_CONTEXT.search(segment)]
    def assess(skill):
        key = normalize(skill)
        terms = ALIASES.get(key, [key])
        present = lambda term: any(contains(segment, term) for segment in usable)
        found = all(present(term) for term in terms) if key == 'html and css' else any(present(term) for term in terms)
        evidence = [s[:240] for s in usable if any(contains(s, t) for t in terms)][:2] if found else []
        return {'skill': skill, 'mentioned': found, 'evidence': evidence}
    required = [assess(item) for item in job['required_skills']]
    preferred = [assess(item) for item in job['preferred_skills']]
    matched = sum(row['mentioned'] for row in required)
    return {'score': round(100 * matched / len(required)) if required else 0,
            'required': required, 'preferred': preferred,
            'evidence_status': 'limited' if matched == len(required) and any(WORK_CONTEXT.search(s) for s in usable) else 'insufficient'}


def check(job, text):
    blind = BlindText(text=text, masked=False)
    actual = _score_blind(job, blind)
    expected = old_score(job, blind)
    assert actual['score'] == expected['score']
    assert actual['evidence_status'] == expected['evidence_status']
    for category in ('required', 'preferred'):
        for result, previous in zip(actual[category], expected[category], strict=True):
            assert (result['skill'], result['mentioned']) == (previous['skill'], previous['mentioned'])
            assert all(len(quote) <= 240 and quote in text for quote in result['evidence'])


@pytest.mark.parametrize('text,skills', [
    ('PythonPython Python Python\nPython next\nPython third', ['Python']),
    ('built HTML CSS HTML CSS\nHTML next\nCSS next', ['HTML and CSS']),
    ('A\nB', ['A B', 'A', 'B', '']),
    ('No SQL; Python\nused SQL! Not Java\nJava', ['SQL', 'Python', 'Java']),
    ('لغة\nاستخدمت Python وHTML\nNoSQL SQL_C SQL', ['Python', 'HTML', 'SQL', 'لغة']),
    ('ＡＰＩ REST\u00a0API\nNode.js .NET', ['API', 'http apis', 'Node.js', '.NET']),
    ('alpha\r\nbeta\u2028gamma\t delta', ['alpha', 'beta', 'gamma delta']),
    ('   \n  ', ['', 'Python']),
])
def test_exact_legacy_scoring_examples(text, skills):
    check({'required_skills': skills, 'preferred_skills': list(reversed(skills))}, text)


def test_deterministic_varied_unicode_and_boundaries():
    rng = random.Random(1042)
    fragments = ['python', 'PythonPython', 'SQL', 'NoSQL', 'node.js', 'HTML', 'CSS', 'html',
                 'not', 'developed', 'ignore', 'لا', 'وHTML', 'ß', 'ﬃ', 'a_b', 'ب', '😀', 'x', '']
    separators = [' ', '\n', '!', '?', '. ', '؛ ', '\t', ',', '\u2028']
    skills = ['Python', 'SQL', 'HTTP APIs', 'HTML and CSS', 'Node.js', 'ß', 'ffi', 'ب', '', 'x']
    for _ in range(300):
        text = ''.join(rng.choice(fragments) + rng.choice(separators) for _ in range(rng.randrange(0, 40)))
        check({'required_skills': rng.sample(skills, rng.randrange(0, len(skills)+1)),
               'preferred_skills': rng.sample(skills, rng.randrange(0, len(skills)+1))}, text)

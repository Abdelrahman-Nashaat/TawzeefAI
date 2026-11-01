"""Highlight limited text cues for a human reviewer, never infer a judgment."""

import re

_CUES = (
    ('negation', re.compile(r"\b(?:no|never|without|lacks?|cannot|can't|not(?!\s+only\b))\b", re.IGNORECASE)),
    ('negation', re.compile(r'(?<!\w)(?:\u0644\u0627|\u0644\u0645|\u0644\u064a\u0633|\u0628\u062f\u0648\u0646)(?!\w)')),
    ('aspiration', re.compile(
        r'\b(?:hope|hopes|want|wants|plan|plans|intend|intends|aim|aims)\s+to\s+'
        r'(?:learn|study|gain|develop|obtain|acquire|practice|build|use)\b', re.IGNORECASE)),
    ('other_person', re.compile(r'\b(?:my|a|the)\s+(?:colleague|coworker|co-worker|teammate|mentor)\b', re.IGNORECASE)),
    ('aspiration', re.compile(
        r'(?<!\w)(?:\u0623\u0631\u063a\u0628|\u0627\u0631\u063a\u0628)\s+'
        r'\u0641\u064a\s+\u062a\u0639\u0644\u0645(?!\w)')),
)
MAX_CAUTIONS = 12


def evidence_cautions(excerpt: str, source_start: int) -> list[dict]:
    """Return bounded exact cue spans in the partially redacted source.

    A cue does not establish its scope or relationship to any job criterion.
    Missing cues do not mean that a passage supports a qualification.
    """
    found = []
    for kind, pattern in _CUES:
        for match in pattern.finditer(excerpt):
            found.append({'kind': kind, 'text': match.group(),
                          'start': source_start + match.start(), 'end': source_start + match.end()})
    return sorted(found, key=lambda item: (item['start'], item['end'], item['kind']))[:MAX_CAUTIONS]

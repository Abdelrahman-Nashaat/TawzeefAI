"""Citation coverage rejects uncited sentences and Unicode line breaks."""
import pytest

from app.knowledge import Source
from app.postgres_knowledge import _has_cited_sentences


@pytest.mark.parametrize('answer', [
    'First claim [pg-doc-1].Unsupported second claim.',
    'First claim [pg-doc-1]\u2028Unsupported line',
    'First claim [pg-doc-1]\u2029Unsupported line',
    'First claim [pg-doc-1]。Unsupported second claim',
    'First claim [pg-doc-1]！Unsupported second claim',
    'First claim [pg-doc-1]？Unsupported second claim',
])
def test_uncited_claim_is_rejected_even_without_ascii_whitespace(answer):
    assert not _has_cited_sentences(answer, [Source('pg-doc-1', 'title', 'text')])


@pytest.mark.parametrize('answer', [
    'First claim [pg-doc-1].Second claim [pg-doc-1].',
    'First claim. [pg-doc-1] Second claim. [pg-doc-1]',
    'The value is 2.5 [pg-doc-1].',
    'First claim [pg-doc-1]。Second claim [pg-doc-1]。',
])
def test_cited_sentences_and_decimal_are_accepted(answer):
    assert _has_cited_sentences(answer, [Source('pg-doc-1', 'title', 'text')])

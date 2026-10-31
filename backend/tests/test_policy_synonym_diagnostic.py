import pytest

from app.policy_synonym_diagnostic import PAIRS, evaluate


class Vector:
    def __init__(self, value):
        self.value = value

    def __matmul__(self, other):
        return self.value * other.value


class FakeEmbedder:
    def encode(self, texts, *, query):
        return [Vector(1 if query else 0.9) for _ in texts]


def test_diagnostic_reports_false_returns_not_just_positive_recall():
    result = evaluate(FakeEmbedder())
    positives = sum(case[4] for case in PAIRS)
    negatives = len(PAIRS) - positives
    assert result['lexical']['supported_returned'] + result['lexical']['supported_abstained'] == positives
    assert result['semantic_candidate']['unsupported_returned'] + result['semantic_candidate']['unsupported_abstained'] == negatives
    assert result['semantic_candidate']['unsupported_returned'] > 0
    assert len({item['id'] for item in result['cases']}) == len(PAIRS)


def test_diagnostic_rejects_mismatched_embeddings():
    class BrokenEmbedder:
        def encode(self, texts, *, query):
            return []
    with pytest.raises(ValueError, match='count'):
        evaluate(BrokenEmbedder())

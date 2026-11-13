"""Bounded fusion keeps semantic-only passages out of answer evidence."""
from app.knowledge import Source
from app.postgres_knowledge import _rerank_lexical_with_semantic


def test_semantic_rank_promotes_a_lexically_eligible_passage():
    first = Source('first', 'Leave', 'Annual leave needs approval.')
    second = Source('second', 'Leave', 'Annual leave is ten days.')
    semantic_only = Source('lead', 'Holiday', 'Vacation rules differ.')
    ranked = _rerank_lexical_with_semantic(
        [(9.0, first), (8.0, second)], [(0.99, semantic_only), (0.92, second)]
    )
    assert [source.id for _, source in ranked] == ['second', 'first']
    assert all(source.id != 'lead' for _, source in ranked)


def test_no_semantic_intersection_preserves_lexical_order():
    first = Source('first', 'One', 'First.')
    second = Source('second', 'Two', 'Second.')
    assert _rerank_lexical_with_semantic([(2, first), (1, second)], []) == [(2, first), (1, second)]
    assert _rerank_lexical_with_semantic([], [(0.9, first)]) == []


def test_duplicate_semantic_ids_cannot_amplify_a_passage():
    first = Source('first', 'One', 'First.')
    second = Source('second', 'Two', 'Second.')
    assert _rerank_lexical_with_semantic([(2, first), (1, second)],
        [(0.9, first), (0.8, second), (0.7, second)]) == [(2, first), (1, second)]

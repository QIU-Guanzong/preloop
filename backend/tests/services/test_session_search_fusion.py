"""Rank fusion of the keyword and vector candidate lists.

The constants are tunable and unvalidated, so what is pinned here is the
behaviour they are chosen to produce: a session both halves liked outranks a
session one half liked, a session is never dropped for being in only one
list, and the same two lists always produce the same order.
"""

from dataclasses import dataclass

from preloop.services.session_search_fusion import (
    KEYWORD_FUSION_WEIGHT,
    MATCH_REASON_BOTH,
    MATCH_REASON_KEYWORD,
    MATCH_REASON_SEMANTIC,
    RRF_K,
    SEMANTIC_FUSION_WEIGHT,
    fuse,
    group_vector_hits,
)


@dataclass
class _Hit:
    """Enough of a chunk hit for the grouping under test."""

    document_id: str
    runtime_session_id: str
    similarity: float


def test_a_session_both_halves_found_outranks_a_session_one_half_found():
    """The whole point of fusing: agreement between the halves wins."""
    fused = fuse(
        keyword_order=["keyword-only", "both"],
        semantic_order=["both", "semantic-only"],
    )

    assert [row.runtime_session_id for row in fused][0] == "both"
    assert {row.runtime_session_id for row in fused} == {
        "both",
        "keyword-only",
        "semantic-only",
    }


def test_each_session_carries_the_half_that_found_it():
    """A fused answer that does not say which half found a row is a guess."""
    fused = {
        row.runtime_session_id: row
        for row in fuse(
            keyword_order=["keyword-only", "both"],
            semantic_order=["both", "semantic-only"],
        )
    }

    assert fused["keyword-only"].match_reason == MATCH_REASON_KEYWORD
    assert fused["semantic-only"].match_reason == MATCH_REASON_SEMANTIC
    assert fused["both"].match_reason == MATCH_REASON_BOTH


def test_the_fused_score_is_the_documented_formula():
    """The score is reciprocal rank fusion, not an invented blend."""
    fused = {
        row.runtime_session_id: row
        for row in fuse(keyword_order=["a", "b"], semantic_order=["b"])
    }

    assert fused["a"].score == KEYWORD_FUSION_WEIGHT / (RRF_K + 1)
    assert fused["b"].score == (
        KEYWORD_FUSION_WEIGHT / (RRF_K + 2) + SEMANTIC_FUSION_WEIGHT / (RRF_K + 1)
    )


def test_a_semantic_only_session_is_never_dropped():
    """A hybrid answer that omits the semantic half is not hybrid."""
    fused = fuse(keyword_order=[], semantic_order=["semantic-only"])

    assert [row.runtime_session_id for row in fused] == ["semantic-only"]
    assert fused[0].match_reason == MATCH_REASON_SEMANTIC


def test_ties_are_broken_the_same_way_every_time():
    """Two sessions at the same fused score keep one documented order."""
    first = fuse(keyword_order=["z-session", "a-session"], semantic_order=[])
    swapped = fuse(keyword_order=["z-session", "a-session"], semantic_order=[])

    assert [row.runtime_session_id for row in first] == [
        row.runtime_session_id for row in swapped
    ]

    tied = fuse(
        keyword_order=["z-session", "a-session"],
        semantic_order=["a-session", "z-session"],
    )
    assert [row.runtime_session_id for row in tied] == ["a-session", "z-session"]


def test_a_tie_is_broken_by_the_keyword_score_before_the_session_id():
    """Identical positions in both lists still have a signal to order by."""
    fused = fuse(
        keyword_order=["a-session", "b-session"],
        semantic_order=["b-session", "a-session"],
        keyword_scores={"a-session": 0.1, "b-session": 0.9},
    )

    assert [row.runtime_session_id for row in fused] == ["b-session", "a-session"]


def test_scores_and_similarities_are_carried_through():
    """The response shows both halves' numbers, not only the fused one."""
    fused = {
        row.runtime_session_id: row
        for row in fuse(
            keyword_order=["a"],
            semantic_order=["a"],
            keyword_scores={"a": 0.42},
            similarities={"a": 0.87},
        )
    }

    assert fused["a"].keyword_score == 0.42
    assert fused["a"].similarity == 0.87


def test_chunk_hits_group_into_sessions_by_their_closest_chunk():
    """A session is as close as its closest chunk, not as its average."""
    grouped = group_vector_hits(
        [
            _Hit("doc-1", "session-far", 0.60),
            _Hit("doc-2", "session-near", 0.95),
            _Hit("doc-3", "session-near", 0.70),
        ]
    )

    assert [row.runtime_session_id for row in grouped] == [
        "session-near",
        "session-far",
    ]
    assert grouped[0].best_similarity == 0.95
    assert grouped[0].chunk_count == 2
    assert grouped[0].document_ids == ["doc-2", "doc-3"]


def test_equally_close_sessions_group_in_a_documented_order():
    """Grouping never inherits the order two equal distances came back in."""
    grouped = group_vector_hits(
        [_Hit("doc-1", "z-session", 0.5), _Hit("doc-2", "a-session", 0.5)]
    )

    assert [row.runtime_session_id for row in grouped] == ["a-session", "z-session"]

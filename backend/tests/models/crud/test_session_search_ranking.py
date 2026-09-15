"""Ranking and fusion behaviour of the session search corpus query.

These tests pin the orderings the endpoint contract promises: relevance
before recency, several matching chunks before one, and the three
``websearch_to_tsquery`` behaviours a user expects from a search box. The
fusion constants themselves are tunable and unvalidated; what is pinned here
is the ordering they are chosen to produce, not the numbers.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from preloop.models.crud import (
    crud_account,
    crud_runtime_session,
    crud_session_search_document,
)
from preloop.models.crud.session_search_document import (
    MAX_SESSION_RESULTS,
    SessionSearchChunk,
    SessionSearchFilters,
    normalize_query,
)
from preloop.models.models.session_search_document import (
    SOURCE_KIND_TOOL_CALL,
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
)

BASE_AT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)


def _session(db_session, account_id, source_id, *, started_at=BASE_AT):
    return crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="custom",
        session_source_id=source_id,
        session_reference=source_id,
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Test Agent",
        started_at=started_at,
        last_activity_at=started_at,
    )


def _write(
    db_session,
    account_id,
    session,
    *texts,
    source_id="message-1",
    source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
    occurred_at=BASE_AT,
    **chunk_fields,
):
    return crud_session_search_document.replace_source_chunks(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        source_kind=source_kind,
        source_id=source_id,
        occurred_at=occurred_at,
        chunks=[
            SessionSearchChunk(
                content=text, chunk_index=index, role="assistant", **chunk_fields
            )
            for index, text in enumerate(texts)
        ],
    )


def _search(db_session, account_id, query, **kwargs):
    return crud_session_search_document.search_sessions_ranked(
        db_session, account_id=account_id, query=query, **kwargs
    )


def test_relevance_beats_recency_when_the_best_match_is_the_oldest(
    db_session, test_user
):
    """The oldest session wins when it is the one that actually matched.

    Ordering by timestamp is the wrong answer to "where did an agent do
    this", so the fixture is built to make the two orderings disagree.
    """
    oldest = _session(db_session, test_user.account_id, "oldest")
    newest = _session(
        db_session,
        test_user.account_id,
        "newest",
        started_at=BASE_AT + timedelta(days=30),
    )
    _write(
        db_session,
        test_user.account_id,
        oldest,
        "we rolled the ledger migration back, then rolled the ledger "
        "migration forward, ledger migration again",
        source_id="oldest-message",
        occurred_at=BASE_AT,
    )
    _write(
        db_session,
        test_user.account_id,
        newest,
        "one passing mention of the ledger and nothing else",
        source_id="newest-message",
        occurred_at=BASE_AT + timedelta(days=30),
    )
    db_session.flush()

    results, total = _search(db_session, test_user.account_id, "ledger")

    assert total == 2
    assert [str(row.runtime_session_id) for row in results] == [
        str(oldest.id),
        str(newest.id),
    ]
    assert results[0].last_match_at < results[1].last_match_at
    assert results[0].score > results[1].score


def test_three_matching_chunks_outrank_one_matching_chunk(db_session, test_user):
    """Matching in several places is the signal, and the fusion keeps it."""
    spread = _session(db_session, test_user.account_id, "spread")
    single = _session(db_session, test_user.account_id, "single")
    _write(
        db_session,
        test_user.account_id,
        spread,
        "the rollout paused",
        "the rollout resumed",
        "the rollout finished",
        source_id="spread-message",
    )
    _write(
        db_session,
        test_user.account_id,
        single,
        "the rollout paused",
        source_id="single-message",
    )
    db_session.flush()

    results, total = _search(db_session, test_user.account_id, "rollout")

    assert total == 2
    assert str(results[0].runtime_session_id) == str(spread.id)
    assert results[0].matched_chunk_count == 3
    assert results[1].matched_chunk_count == 1
    # Identical chunk text means identical per chunk rank: the only thing
    # separating the two is the fusion.
    assert results[0].best_chunk_rank == results[1].best_chunk_rank
    assert results[0].score > results[1].score


def test_a_quoted_phrase_matches_only_the_adjacent_words(db_session, test_user):
    """`"rolling restart"` is a phrase, not two independent terms."""
    adjacent = _session(db_session, test_user.account_id, "adjacent")
    scattered = _session(db_session, test_user.account_id, "scattered")
    _write(
        db_session,
        test_user.account_id,
        adjacent,
        "performed a rolling restart of the workers",
        source_id="adjacent-message",
    )
    _write(
        db_session,
        test_user.account_id,
        scattered,
        "rolling upgrades finished before we had to restart anything",
        source_id="scattered-message",
    )
    db_session.flush()

    results, total = _search(db_session, test_user.account_id, '"rolling restart"')

    assert total == 1
    assert [str(row.runtime_session_id) for row in results] == [str(adjacent.id)]

    # Unquoted, the same two words are an AND and both sessions match.
    _, unquoted_total = _search(db_session, test_user.account_id, "rolling restart")
    assert unquoted_total == 2


def test_alternation_returns_either_side(db_session, test_user):
    """`a or b` widens to either term, and a session with neither is out."""
    left = _session(db_session, test_user.account_id, "left")
    right = _session(db_session, test_user.account_id, "right")
    neither = _session(db_session, test_user.account_id, "neither")
    _write(
        db_session,
        test_user.account_id,
        left,
        "the migration completed",
        source_id="left-message",
    )
    _write(
        db_session,
        test_user.account_id,
        right,
        "the rollback completed",
        source_id="right-message",
    )
    _write(
        db_session,
        test_user.account_id,
        neither,
        "the deployment completed",
        source_id="neither-message",
    )
    db_session.flush()

    results, total = _search(db_session, test_user.account_id, "migration or rollback")

    assert total == 2
    assert {str(row.runtime_session_id) for row in results} == {
        str(left.id),
        str(right.id),
    }


def test_a_leading_minus_excludes_the_term(db_session, test_user):
    """`deploy -staging` keeps the deploys that are not about staging."""
    production = _session(db_session, test_user.account_id, "production")
    staging = _session(db_session, test_user.account_id, "staging")
    _write(
        db_session,
        test_user.account_id,
        production,
        "deploy to production succeeded",
        source_id="production-message",
    )
    _write(
        db_session,
        test_user.account_id,
        staging,
        "deploy to staging succeeded",
        source_id="staging-message",
    )
    db_session.flush()

    results, total = _search(db_session, test_user.account_id, "deploy -staging")

    assert total == 1
    assert [str(row.runtime_session_id) for row in results] == [str(production.id)]

    # Without the exclusion both sessions are hits, so the filtering is the
    # exclusion operator and not an accident of the fixture.
    _, unfiltered_total = _search(db_session, test_user.account_id, "deploy")
    assert unfiltered_total == 2


def test_the_account_bound_holds_with_and_without_filters(db_session, test_user):
    """Another account's chunks are unreachable however the query is shaped."""
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    mine = _session(db_session, test_user.account_id, "mine")
    theirs = _session(db_session, other_account.id, "theirs")
    _write(
        db_session,
        test_user.account_id,
        mine,
        "the checkout ledger reconciled",
        source_id="mine-message",
        provider_name="openai",
    )
    _write(
        db_session,
        other_account.id,
        theirs,
        "the checkout ledger reconciled",
        source_id="theirs-message",
        provider_name="openai",
    )
    db_session.flush()

    unfiltered, unfiltered_total = _search(db_session, test_user.account_id, "ledger")
    filtered, filtered_total = _search(
        db_session,
        test_user.account_id,
        "ledger",
        filters=SessionSearchFilters(provider_name="openai"),
    )

    assert unfiltered_total == 1
    assert filtered_total == 1
    assert [str(row.runtime_session_id) for row in unfiltered] == [str(mine.id)]
    assert [str(row.runtime_session_id) for row in filtered] == [str(mine.id)]


def test_snippets_carry_the_identity_needed_to_reopen_the_turn(db_session, test_user):
    """A snippet names its session, its turn and the chunk inside it."""
    session = _session(db_session, test_user.account_id, "identity")
    _write(
        db_session,
        test_user.account_id,
        session,
        "grep the audit ledger for the failed write",
        source_id="tool-call-7",
        source_kind=SOURCE_KIND_TOOL_CALL,
        occurred_at=BASE_AT + timedelta(minutes=5),
    )
    db_session.flush()

    results, _ = _search(db_session, test_user.account_id, "ledger")

    snippet = results[0].snippets[0]
    assert str(snippet.runtime_session_id) == str(session.id)
    assert snippet.source_kind == SOURCE_KIND_TOOL_CALL
    assert snippet.source_id == "tool-call-7"
    assert snippet.chunk_index == 0
    assert snippet.occurred_at == BASE_AT + timedelta(minutes=5)
    assert "<mark>ledger</mark>" in snippet.text


def test_disabling_snippet_text_still_returns_snippet_identity(db_session, test_user):
    """Identity is cheap and useful; the captured text is the part withheld."""
    session = _session(db_session, test_user.account_id, "quiet")
    _write(
        db_session,
        test_user.account_id,
        session,
        "a secret sounding sentence about the ledger",
        source_id="quiet-message",
    )
    db_session.flush()

    results, _ = _search(
        db_session, test_user.account_id, "ledger", include_snippet_text=False
    )

    snippet = results[0].snippets[0]
    assert snippet.text is None
    assert snippet.source_id == "quiet-message"
    assert snippet.rank > 0


def test_zero_snippets_returns_scored_sessions_with_no_snippet_rows(
    db_session, test_user
):
    """A caller that only wants the ranking does not pay for headlines."""
    session = _session(db_session, test_user.account_id, "scores-only")
    _write(
        db_session,
        test_user.account_id,
        session,
        "the ledger reconciled",
        source_id="scores-message",
    )
    db_session.flush()

    results, total = _search(
        db_session, test_user.account_id, "ledger", max_snippets_per_session=0
    )

    assert total == 1
    assert results[0].snippets == []
    assert results[0].score > 0


def test_paging_is_stable_and_total_counts_sessions_not_chunks(db_session, test_user):
    """``total`` is the number of sessions a caller pages through."""
    for index in range(3):
        session = _session(db_session, test_user.account_id, f"page-{index}")
        _write(
            db_session,
            test_user.account_id,
            session,
            "ledger entry one",
            "ledger entry two",
            source_id=f"page-message-{index}",
        )
    db_session.flush()

    first_page, total = _search(db_session, test_user.account_id, "ledger", limit=2)
    second_page, second_total = _search(
        db_session, test_user.account_id, "ledger", limit=2, offset=2
    )

    assert total == 3
    assert second_total == 3
    assert len(first_page) == 2
    assert len(second_page) == 1
    seen = {str(row.runtime_session_id) for row in first_page + second_page}
    assert len(seen) == 3


def test_a_limit_above_the_documented_maximum_is_clamped(db_session, test_user):
    """The CRUD layer never runs a page larger than the documented ceiling."""
    session = _session(db_session, test_user.account_id, "clamped")
    _write(
        db_session,
        test_user.account_id,
        session,
        "ledger",
        source_id="clamped-message",
    )
    db_session.flush()

    results, total = _search(
        db_session, test_user.account_id, "ledger", limit=MAX_SESSION_RESULTS * 10
    )

    assert total == 1
    assert len(results) == 1


def test_an_empty_query_matches_nothing_rather_than_everything(db_session, test_user):
    """A blank query is not a request for the whole corpus."""
    session = _session(db_session, test_user.account_id, "blank")
    _write(
        db_session, test_user.account_id, session, "ledger", source_id="blank-message"
    )
    db_session.flush()

    assert _search(db_session, test_user.account_id, "   ") == ([], 0)
    assert normalize_query("  rolling   restart  ") == "rolling restart"
    assert normalize_query("   ") is None


def test_filters_narrow_within_the_account(db_session, test_user):
    """Each denormalised column filters the corpus without a source join."""
    flow_id = uuid4()
    matched = _session(db_session, test_user.account_id, "matched")
    other = _session(db_session, test_user.account_id, "other")
    _write(
        db_session,
        test_user.account_id,
        matched,
        "ledger reconciliation",
        source_id="matched-message",
        model_alias="vendor/model-a",
        provider_name="vendor",
        runtime_principal_id="agent-7",
        flow_id=flow_id,
    )
    _write(
        db_session,
        test_user.account_id,
        other,
        "ledger reconciliation",
        source_id="other-message",
        model_alias="vendor/model-b",
        provider_name="other-vendor",
        runtime_principal_id="agent-8",
    )
    db_session.flush()

    for filters in (
        SessionSearchFilters(model_alias="vendor/model-a"),
        SessionSearchFilters(provider_name="vendor"),
        SessionSearchFilters(runtime_principal_id="agent-7"),
        SessionSearchFilters(flow_id=flow_id),
        SessionSearchFilters(
            source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE, flow_id=flow_id
        ),
    ):
        results, total = _search(
            db_session, test_user.account_id, "ledger", filters=filters
        )
        assert total == 1, filters
        assert [str(row.runtime_session_id) for row in results] == [str(matched.id)]


def test_indexed_through_reports_the_newest_indexed_content(db_session, test_user):
    """The freshness marker is per account and does not depend on a query."""
    session = _session(db_session, test_user.account_id, "fresh")
    _write(
        db_session,
        test_user.account_id,
        session,
        "ledger one",
        source_id="fresh-old",
        occurred_at=BASE_AT,
    )
    _write(
        db_session,
        test_user.account_id,
        session,
        "unrelated text",
        source_id="fresh-new",
        occurred_at=BASE_AT + timedelta(hours=3),
    )
    db_session.flush()

    marker = crud_session_search_document.indexed_through(
        db_session, account_id=test_user.account_id
    )

    assert marker == BASE_AT + timedelta(hours=3)

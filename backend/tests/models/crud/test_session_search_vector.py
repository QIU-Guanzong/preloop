"""Vector retrieval over the session search corpus.

These tests pin the properties a semantic answer depends on and a refactor
could quietly lose: a query never scores a vector from another model, a
withheld chunk cannot answer with the text it no longer has, the ordering is
the same twice in a row, and the coverage counts a degraded marker is built
from are the counts they claim to be.
"""

from datetime import datetime, timedelta, timezone

from preloop.models.crud import (
    crud_account,
    crud_runtime_session,
    crud_session_search_document,
)
from preloop.models.crud.session_search_document import (
    MIN_SEMANTIC_SIMILARITY,
    SessionSearchChunk,
    SessionSearchFilters,
)
from preloop.models.models.session_search_document import (
    EMBEDDING_DIMENSIONS,
    REDACTION_STATE_WITHHELD,
    SOURCE_KIND_SESSION_SUMMARY,
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
)

BASE_AT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
MODEL_A = "local:model-a@1536"
MODEL_B = "local:model-b@1536"


def _axis(index: int) -> list[float]:
    """A unit vector on one axis, so similarities are exact and obvious."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[index] = 1.0
    return vector


def _blend(first: int, second: int) -> list[float]:
    """A vector halfway between two axes: cosine 0.7071 with either."""
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[first] = 1.0
    vector[second] = 1.0
    return vector


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
    text,
    *,
    source_id="message-1",
    occurred_at=BASE_AT,
    vector=None,
    model_identity=MODEL_A,
    source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
    **chunk_fields,
):
    """Write one chunk and, unless told otherwise, give it a vector."""
    rows = crud_session_search_document.replace_source_chunks(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        source_kind=source_kind,
        source_id=source_id,
        occurred_at=occurred_at,
        chunks=[SessionSearchChunk(content=text, role="assistant", **chunk_fields)],
    )
    if vector is not None:
        crud_session_search_document.store_embeddings(
            db_session,
            vectors=[(rows[0], vector)],
            model_identity=model_identity,
        )
    db_session.flush()
    return rows[0]


def _chunks(db_session, account_id, vector, **kwargs):
    return crud_session_search_document.search_vector_chunks(
        db_session,
        account_id=account_id,
        embedding=vector,
        embedding_model=kwargs.pop("embedding_model", MODEL_A),
        **kwargs,
    )


def test_a_query_never_scores_a_chunk_embedded_with_another_model(
    db_session, test_user
):
    """Two models' spaces are not one space, so a query stays in its own.

    Both chunks here carry the identical vector. The only difference is the
    model identity stamped on them, and that difference alone decides which
    one a query may see.
    """
    mine = _session(db_session, test_user.account_id, "same-model")
    theirs = _session(db_session, test_user.account_id, "other-model")
    _write(
        db_session,
        test_user.account_id,
        mine,
        "rolled the release back",
        source_id="same-model-message",
        vector=_axis(3),
        model_identity=MODEL_A,
    )
    _write(
        db_session,
        test_user.account_id,
        theirs,
        "rolled the release back",
        source_id="other-model-message",
        vector=_axis(3),
        model_identity=MODEL_B,
    )

    hits = _chunks(db_session, test_user.account_id, _axis(3))

    assert [str(hit.runtime_session_id) for hit in hits] == [str(mine.id)]
    assert hits[0].similarity > 0.99

    # And the other way round, so this is a restriction and not an ordering.
    other = _chunks(db_session, test_user.account_id, _axis(3), embedding_model=MODEL_B)
    assert [str(hit.runtime_session_id) for hit in other] == [str(theirs.id)]


def test_chunks_below_the_similarity_floor_are_not_matches(db_session, test_user):
    """Without a floor, every query returns the corpus in distance order."""
    near = _session(db_session, test_user.account_id, "near")
    far = _session(db_session, test_user.account_id, "far")
    _write(
        db_session,
        test_user.account_id,
        near,
        "the deployment was rolled back",
        source_id="near-message",
        vector=_blend(5, 6),
    )
    _write(
        db_session,
        test_user.account_id,
        far,
        "an unrelated conversation about lunch",
        source_id="far-message",
        vector=_axis(9),
    )

    hits = _chunks(db_session, test_user.account_id, _axis(5))

    assert [str(hit.runtime_session_id) for hit in hits] == [str(near.id)]
    assert hits[0].similarity > MIN_SEMANTIC_SIMILARITY


def test_the_vector_ordering_is_the_same_twice(db_session, test_user):
    """Equally close chunks come back in the same order every time."""
    sessions = []
    for index in range(4):
        session = _session(db_session, test_user.account_id, f"tied-{index}")
        _write(
            db_session,
            test_user.account_id,
            session,
            f"identical distance chunk {index}",
            source_id=f"tied-message-{index}",
            vector=_axis(11),
        )
        sessions.append(session)

    first = _chunks(db_session, test_user.account_id, _axis(11))
    second = _chunks(db_session, test_user.account_id, _axis(11))

    assert len(first) == len(sessions)
    assert [str(hit.document_id) for hit in first] == [
        str(hit.document_id) for hit in second
    ]


def test_a_chunk_withheld_after_it_was_embedded_is_not_a_semantic_match(
    db_session, test_user
):
    """A redaction that leaves the vector answering is not a redaction.

    The stored text is cleared when a source is withheld, but the vector it
    produced is still on the row. Without the redaction state term in the
    query, a semantic search would keep surfacing the chunk whose content was
    taken away.
    """
    session = _session(db_session, test_user.account_id, "withheld")
    _write(
        db_session,
        test_user.account_id,
        session,
        "the credential was pasted into the transcript",
        source_id="withheld-message",
        vector=_axis(13),
    )
    crud_session_search_document.withhold_source_text(
        db_session,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_id="withheld-message",
    )

    hits = _chunks(db_session, test_user.account_id, _axis(13))

    assert hits == []


def test_another_accounts_vectors_are_never_returned(db_session, test_user):
    """The account bound is in the vector query, as in every other query."""
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    theirs = _session(db_session, other_account.id, "theirs-vector")
    _write(
        db_session,
        other_account.id,
        theirs,
        "the ledger reconciled cleanly",
        source_id="theirs-vector-message",
        vector=_axis(17),
    )

    hits = _chunks(db_session, test_user.account_id, _axis(17))

    assert hits == []


def test_filters_narrow_the_vector_pass_the_way_they_narrow_the_keyword_pass(
    db_session, test_user
):
    """One filter block, applied to both halves, or a hybrid answer is two."""
    kept = _session(db_session, test_user.account_id, "kept")
    dropped = _session(db_session, test_user.account_id, "dropped")
    _write(
        db_session,
        test_user.account_id,
        kept,
        "a summary of the incident",
        source_id="kept-message",
        vector=_axis(19),
        provider_name="vendor-a",
    )
    _write(
        db_session,
        test_user.account_id,
        dropped,
        "a summary of the incident",
        source_id="dropped-message",
        vector=_axis(19),
        provider_name="vendor-b",
    )

    hits = _chunks(
        db_session,
        test_user.account_id,
        _axis(19),
        filters=SessionSearchFilters(provider_name="vendor-a"),
    )

    assert [str(hit.runtime_session_id) for hit in hits] == [str(kept.id)]


def test_embedding_coverage_counts_what_a_degraded_marker_claims(db_session, test_user):
    """The counts behind "no vectors", "wrong model" and "not yet"."""
    session = _session(db_session, test_user.account_id, "coverage")
    _write(
        db_session,
        test_user.account_id,
        session,
        "embedded with the query's model",
        source_id="coverage-one",
        occurred_at=BASE_AT,
        vector=_axis(21),
        model_identity=MODEL_A,
    )
    _write(
        db_session,
        test_user.account_id,
        session,
        "embedded with another model",
        source_id="coverage-two",
        occurred_at=BASE_AT + timedelta(hours=2),
        vector=_axis(22),
        model_identity=MODEL_B,
    )
    _write(
        db_session,
        test_user.account_id,
        session,
        "still waiting for a vector",
        source_id="coverage-three",
        occurred_at=BASE_AT + timedelta(hours=4),
    )

    coverage = crud_session_search_document.embedding_coverage(
        db_session, account_id=test_user.account_id, embedding_model=MODEL_A
    )

    assert coverage.vectors == 2
    assert coverage.model_vectors == 1
    assert coverage.pending == 1
    assert coverage.embedded_through == BASE_AT


def test_embedding_coverage_pending_honours_source_kinds(db_session, test_user):
    """Out-of-scope chunks are not the backfill the search marker talks about."""
    session = _session(db_session, test_user.account_id, "coverage-scope")
    _write(
        db_session,
        test_user.account_id,
        session,
        "the session summary that is in scope",
        source_id="coverage-summary",
        occurred_at=BASE_AT,
        vector=_axis(21),
        source_kind=SOURCE_KIND_SESSION_SUMMARY,
    )
    _write(
        db_session,
        test_user.account_id,
        session,
        "a transcript turn this account chose not to embed",
        source_id="coverage-transcript",
        occurred_at=BASE_AT + timedelta(hours=1),
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
    )

    whole = crud_session_search_document.embedding_coverage(
        db_session, account_id=test_user.account_id, embedding_model=MODEL_A
    )
    scoped = crud_session_search_document.embedding_coverage(
        db_session,
        account_id=test_user.account_id,
        embedding_model=MODEL_A,
        source_kinds=(SOURCE_KIND_SESSION_SUMMARY,),
    )

    assert whole.pending == 1
    assert scoped.pending == 0
    assert scoped.vectors == 1
    assert scoped.model_vectors == 1


def test_coverage_is_account_scoped(db_session, test_user):
    """Another account's vectors are not this account's coverage."""
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    theirs = _session(db_session, other_account.id, "theirs-coverage")
    _write(
        db_session,
        other_account.id,
        theirs,
        "their embedded content",
        source_id="theirs-coverage-message",
        vector=_axis(23),
    )

    coverage = crud_session_search_document.embedding_coverage(
        db_session, account_id=test_user.account_id, embedding_model=MODEL_A
    )

    assert coverage.vectors == 0
    assert coverage.model_vectors == 0
    assert coverage.pending == 0
    assert coverage.embedded_through is None


def test_snippet_text_for_a_semantically_matched_chunk_is_its_opening(
    db_session, test_user
):
    """A chunk with none of the query's words still has something to show."""
    session = _session(db_session, test_user.account_id, "semantic-snippet")
    row = _write(
        db_session,
        test_user.account_id,
        session,
        "we spent the afternoon untangling the deployment and then went home",
        source_id="semantic-snippet-message",
        vector=_axis(27),
    )

    texts = crud_session_search_document.snippet_text_for_documents(
        db_session,
        account_id=test_user.account_id,
        document_ids=[row.id],
        query="ledger",
    )

    assert texts[str(row.id)]
    assert "afternoon" in texts[str(row.id)]


def test_snippet_text_is_never_returned_for_a_withheld_chunk(db_session, test_user):
    """The one state whose text stopped being allowed stays unreadable."""
    session = _session(db_session, test_user.account_id, "withheld-snippet")
    row = _write(
        db_session,
        test_user.account_id,
        session,
        "the credential was pasted into the transcript",
        source_id="withheld-snippet-message",
        vector=_axis(29),
    )
    crud_session_search_document.withhold_source_text(
        db_session,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_id="withheld-snippet-message",
    )
    db_session.refresh(row)
    assert row.redaction_state == REDACTION_STATE_WITHHELD

    texts = crud_session_search_document.snippet_text_for_documents(
        db_session,
        account_id=test_user.account_id,
        document_ids=[row.id],
        query="credential",
    )

    assert texts[str(row.id)] is None


def test_session_identities_are_account_scoped(db_session, test_user):
    """A session id from another account resolves to nothing."""
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    mine = _session(db_session, test_user.account_id, "identity-mine")
    theirs = _session(db_session, other_account.id, "identity-theirs")

    identities = crud_session_search_document.session_identities(
        db_session,
        account_id=test_user.account_id,
        session_ids=[mine.id, theirs.id],
    )

    assert list(identities) == [str(mine.id)]
    assert identities[str(mine.id)].session_reference == "identity-mine"

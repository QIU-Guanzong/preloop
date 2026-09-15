"""Tests for the chunked runtime session search corpus CRUD layer."""

from datetime import datetime, timezone

from sqlalchemy import text

from preloop.models.crud import (
    crud_account,
    crud_runtime_session,
    crud_session_search_document,
)
from preloop.models.crud.session_search_document import (
    SessionSearchChunk,
    content_hash_for,
)
from preloop.models.models.session_search_document import (
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
)

OCCURRED_AT = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def _session(db_session, account_id, *, source_id="session-a"):
    return crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="custom",
        session_source_id=source_id,
        session_reference=source_id,
        runtime_principal_type="agent",
        runtime_principal_id="agent-1",
        runtime_principal_name="Test Agent",
        started_at=OCCURRED_AT,
        last_activity_at=OCCURRED_AT,
    )


def _chunks(*texts):
    return [
        SessionSearchChunk(content=value, chunk_index=index)
        for index, value in enumerate(texts)
    ]


def _write(db_session, account_id, session, *texts, source_id="message-1"):
    return crud_session_search_document.replace_source_chunks(
        db_session,
        account_id=account_id,
        runtime_session_id=session.id,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_id=source_id,
        occurred_at=OCCURRED_AT,
        chunks=_chunks(*texts),
    )


def test_stored_vector_is_generated_without_an_explicit_write(db_session, test_user):
    """The tsvector column is populated by the database, not by the writer."""
    session = _session(db_session, test_user.account_id)
    stored = _write(db_session, test_user.account_id, session, "deploy the ledger")

    assert len(stored) == 1
    vector = db_session.execute(
        text("SELECT search_vector FROM session_search_document WHERE id = :id"),
        {"id": stored[0].id},
    ).scalar_one()
    assert "ledger" in vector
    matched = crud_session_search_document.search_account_chunks(
        db_session, account_id=test_user.account_id, query="ledger"
    )
    assert [row.id for row in matched] == [stored[0].id]
    assert stored[0].content_hash == content_hash_for("deploy the ledger")


def test_chunks_are_never_visible_to_another_account(db_session, test_user):
    """An account filter bounds the corpus before anything else applies."""
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Organization", "is_active": True},
    )
    db_session.flush()
    mine = _session(db_session, test_user.account_id)
    theirs = _session(db_session, other_account.id, source_id="session-b")
    _write(db_session, test_user.account_id, mine, "shared vocabulary here")
    _write(
        db_session,
        other_account.id,
        theirs,
        "shared vocabulary here",
        source_id="message-2",
    )

    mine_hits = crud_session_search_document.search_account_chunks(
        db_session, account_id=test_user.account_id, query="vocabulary"
    )
    theirs_hits = crud_session_search_document.search_account_chunks(
        db_session, account_id=other_account.id, query="vocabulary"
    )

    assert [row.runtime_session_id for row in mine_hits] == [mine.id]
    assert [row.runtime_session_id for row in theirs_hits] == [theirs.id]
    # Even naming the other account's session explicitly returns nothing.
    assert (
        crud_session_search_document.search_account_chunks(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=theirs.id,
        )
        == []
    )
    assert (
        crud_session_search_document.count_for_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=theirs.id,
        )
        == 0
    )


def test_unchanged_content_hash_is_a_no_op_and_a_change_replaces(db_session, test_user):
    """Re-indexing writes nothing unless the content hash moved."""
    session = _session(db_session, test_user.account_id)
    first = _write(db_session, test_user.account_id, session, "one", "two")
    first_ids = sorted(str(row.id) for row in first)

    repeat = _write(db_session, test_user.account_id, session, "one", "two")

    assert sorted(str(row.id) for row in repeat) == first_ids
    assert (
        crud_session_search_document.count_for_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
        )
        == 2
    )

    changed = _write(db_session, test_user.account_id, session, "one", "three")

    assert sorted(str(row.id) for row in changed) != first_ids
    assert (
        crud_session_search_document.count_for_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
        )
        == 2
    )
    assert [row.content for row in changed] == ["one", "three"]

    shrunk = _write(db_session, test_user.account_id, session, "one")

    assert len(shrunk) == 1
    assert (
        crud_session_search_document.count_for_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
        )
        == 1
    )


def test_deleting_a_runtime_session_deletes_its_chunks(db_session, test_user):
    """The session foreign key cascades, so chunks cannot outlive a session."""
    session = _session(db_session, test_user.account_id)
    _write(db_session, test_user.account_id, session, "content to delete")
    session_id = session.id

    db_session.execute(
        text("DELETE FROM runtime_session WHERE id = :id"), {"id": session_id}
    )
    db_session.flush()

    assert (
        crud_session_search_document.count_for_session(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session_id,
        )
        == 0
    )

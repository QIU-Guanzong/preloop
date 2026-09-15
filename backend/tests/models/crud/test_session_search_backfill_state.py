"""The backfill watermark row: one per account, and it only moves backwards."""

import uuid
from datetime import datetime, timedelta, timezone

from preloop.models.crud import (
    crud_account,
    crud_session_search_backfill_state as crud_state,
)

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


def test_get_or_create_keeps_one_row_per_account(db_session, test_user):
    """A second call returns the same row rather than a second one."""
    first = crud_state.get_or_create(db_session, account_id=test_user.account_id)
    second = crud_state.get_or_create(db_session, account_id=test_user.account_id)

    assert first.id == second.id
    assert first.cursor_started_at is None
    assert first.completed_at is None


def test_the_watermark_only_moves_backwards(db_session, test_user):
    """A pass that walked nothing new must not rewind the cursor."""
    older = NOW - timedelta(days=10)
    session_id = uuid.uuid4()
    crud_state.record_pass(
        db_session,
        account_id=test_user.account_id,
        cursor_started_at=older,
        cursor_session_id=session_id,
        sessions_scanned=2,
        rows_written=5,
        now=NOW,
    )

    state = crud_state.record_pass(
        db_session,
        account_id=test_user.account_id,
        cursor_started_at=NOW,
        cursor_session_id=uuid.uuid4(),
        sessions_scanned=1,
        rows_written=1,
        now=NOW,
    )

    assert state.cursor_started_at.replace(tzinfo=timezone.utc) == older
    assert state.cursor_session_id == session_id
    # Counters still accumulate: the work happened, the cursor just stayed.
    assert state.sessions_scanned == 3
    assert state.rows_written == 6


def test_completion_is_stamped_once(db_session, test_user):
    """A second completed pass does not restamp the account."""
    first = crud_state.record_pass(
        db_session,
        account_id=test_user.account_id,
        completed=True,
        now=NOW,
    )
    completed_at = first.completed_at

    second = crud_state.record_pass(
        db_session,
        account_id=test_user.account_id,
        completed=True,
        now=NOW + timedelta(hours=1),
    )

    assert second.completed_at == completed_at


def test_record_error_keeps_the_watermark(db_session, test_user):
    """A failure is recorded without losing the progress already made."""
    older = NOW - timedelta(days=3)
    crud_state.record_pass(
        db_session,
        account_id=test_user.account_id,
        cursor_started_at=older,
        cursor_session_id=uuid.uuid4(),
        now=NOW,
    )

    state = crud_state.record_error(
        db_session,
        account_id=test_user.account_id,
        error="RuntimeError: session scan blew up",
        now=NOW,
    )

    assert state.cursor_started_at.replace(tzinfo=timezone.utc) == older
    assert "session scan blew up" in state.last_error


def test_pending_accounts_exclude_completed_and_inactive_ones(db_session, test_user):
    """The pass reads one row per account, and a finished one drops out."""
    done = crud_account.create(
        db_session,
        obj_in={"organization_name": "Done Organization", "is_active": True},
    )
    inactive = crud_account.create(
        db_session,
        obj_in={"organization_name": "Closed Organization", "is_active": False},
    )
    crud_state.record_pass(db_session, account_id=done.id, completed=True, now=NOW)

    pending = crud_state.pending_account_ids(
        db_session, account_ids=[test_user.account_id, done.id, inactive.id]
    )

    assert test_user.account_id in pending
    assert done.id not in pending
    assert inactive.id not in pending


def test_accounts_never_walked_come_first(db_session, test_user):
    """No account is starved by one that was just given a pass."""
    fresh = crud_account.create(
        db_session,
        obj_in={"organization_name": "Fresh Organization", "is_active": True},
    )
    crud_state.record_pass(
        db_session, account_id=test_user.account_id, rows_written=1, now=NOW
    )

    pending = crud_state.pending_account_ids(
        db_session, account_ids=[test_user.account_id, fresh.id]
    )

    assert pending.index(fresh.id) < pending.index(test_user.account_id)

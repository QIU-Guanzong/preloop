"""A launch grant initializes consumed usage once; it never resets balances."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from preloop.models import models
from preloop.models.crud import hosted_spend as ledger

NOW = datetime(2026, 9, 14, tzinfo=UTC)


def test_preview_then_apply_once_and_preserve_live_hold(db_session, test_user):
    account_id = test_user.account_id
    args = dict(account_id=account_id, now=NOW, evidence="Approved zero-start launch")
    assert ledger.initialize_zero_launch(db_session, **args) == "would_initialize"
    assert (
        ledger.snapshot(db_session, account_id=account_id, now=NOW)["lifetime_spent"]
        is None
    )
    result = ledger.initialize_zero_launch(db_session, **args, apply=True)
    assert result == "initialized"
    # A Free plan still supplies its full $0.50 allowance. Zero means spent.
    reservation = ledger.reserve(
        db_session,
        account_id=account_id,
        operation_key="first",
        amount="0.1",
        lifetime_limit="0.5",
        monthly_limit=None,
        now=NOW,
    )
    before = ledger.snapshot(db_session, account_id=account_id, now=NOW)
    assert before["lifetime_spent"] == 0
    assert before["lifetime_reserved"] == Decimal("0.1")
    repeated = ledger.initialize_zero_launch(db_session, **args, apply=True)
    assert repeated == "already_initialized"
    assert ledger.snapshot(db_session, account_id=account_id, now=NOW) == before
    assert reservation.reserved == Decimal("0.1")


@pytest.mark.parametrize("partial", ["wallet", "month", "reservation"])
def test_partial_state_is_never_repaired_by_zeroing(db_session, test_user, partial):
    account_id = test_user.account_id
    if partial == "wallet":
        row = models.HostedSpendAccount(
            account_id=account_id,
            lifetime_spent=None,
            lifetime_reserved=2,
            coverage_start=None,
        )
    elif partial == "month":
        row = models.HostedSpendMonth(
            account_id=account_id, period=NOW.date().replace(day=1), spent=2, reserved=0
        )
    else:
        row = models.HostedSpendReservation(
            account_id=account_id,
            operation_key="orphan",
            period=NOW.date().replace(day=1),
            reserved=2,
            status="dispatched",
        )
    db_session.add(row)
    db_session.flush()
    for apply in [False, True]:
        result = ledger.initialize_zero_launch(
            db_session, account_id=account_id, now=NOW, evidence="launch", apply=apply
        )
        assert result == "existing_state_requires_review"
    assert ledger._wallet(db_session, account_id) is (
        row if partial == "wallet" else None
    )


def test_launch_page_bound_and_cursor(db_session, test_user):
    first = ledger.launch_account_ids(db_session, limit=1)
    assert len(first) <= 1
    if first:
        assert all(
            identifier > first[0]
            for identifier in ledger.launch_account_ids(
                db_session, after_id=first[0], limit=2
            )
        )
    with pytest.raises(ValueError):
        ledger.launch_account_ids(db_session, limit=501)


def test_existing_wallet_current_unknown_month_is_not_reported_ready(
    db_session, test_user, monkeypatch
):
    from types import SimpleNamespace

    args = dict(account_id=test_user.account_id, now=NOW, evidence="approved launch")
    result = ledger.initialize_zero_launch(db_session, **args, apply=True)
    assert result == "initialized"
    monkeypatch.setattr(
        ledger, "_month", lambda *args: SimpleNamespace(spent=None, reserved=2)
    )
    repeated = ledger.initialize_zero_launch(db_session, **args, apply=True)
    assert repeated == "existing_state_requires_review"


def test_initialized_wallet_next_month_needs_no_reset(db_session, test_user):
    args = dict(account_id=test_user.account_id, evidence="approved launch", apply=True)
    first = ledger.initialize_zero_launch(db_session, **args, now=NOW)
    repeated = ledger.initialize_zero_launch(
        db_session, **args, now=datetime(2026, 10, 1, tzinfo=UTC)
    )
    assert (first, repeated) == ("initialized", "already_initialized")

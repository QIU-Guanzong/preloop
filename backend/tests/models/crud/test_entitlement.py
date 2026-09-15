"""An expired trial entitles nobody, in memory and in SQL.

A staging account whose Stripe trial ended in July still carried
``status == "trialing"`` because the provider webhook never arrived. Every
entitlement lookup checked the status alone, so the account kept a paid plan's
allowances indefinitely. These tests pin the shared rule and both lookups that
apply it.
"""

from datetime import datetime, timedelta, timezone

import pytest

from preloop.models import models
from preloop.models.crud.billing import ENTITLED_STATUSES, billing
from preloop.models.crud.entitlement import (
    ACTIVE_STATUSES,
    entitlement_clause,
    is_entitled_subscription,
    is_expired_trial,
    is_live_trial,
    is_stale_subscription,
    trial_ended_at,
)
from preloop.models.crud.plan import (
    plan as crud_plan,
    subscription as crud_subscription,
)

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
PAST = NOW - timedelta(days=50)
FUTURE = NOW + timedelta(days=13)


def _row(status, period_end):
    return models.Subscription(
        status=status,
        current_period_start=period_end - timedelta(days=14),
        current_period_end=period_end,
    )


@pytest.mark.parametrize(
    "status, period_end, entitled",
    [
        # The bug: a trial whose provider-side end has passed.
        ("trialing", PAST, False),
        ("trialing", FUTURE, True),
        # A paid subscription we simply have not re-read is still paid.
        ("active", PAST, True),
        ("active", FUTURE, True),
        # Dunning keeps working; Stripe is still retrying the card.
        ("past_due", PAST, True),
        ("past_due", FUTURE, True),
        ("canceled", FUTURE, False),
        ("paused", FUTURE, False),
    ],
)
def test_entitlement_rule(status, period_end, entitled):
    assert is_entitled_subscription(_row(status, period_end), now=NOW) is entitled


def test_no_subscription_is_never_entitled():
    assert is_entitled_subscription(None, now=NOW) is False
    assert is_expired_trial(None, now=NOW) is False
    assert is_live_trial(None, now=NOW) is False
    assert trial_ended_at(None) is None
    assert is_stale_subscription(None, now=NOW) is False


@pytest.mark.parametrize(
    "status, period_end, live",
    [
        ("trialing", PAST, False),
        ("trialing", FUTURE, True),
        ("active", FUTURE, False),
        ("past_due", FUTURE, False),
        (None, None, False),
    ],
)
def test_is_live_trial(status, period_end, live):
    subscription = None if status is None else _row(status, period_end)
    assert is_live_trial(subscription, now=NOW) is live


def test_trial_expiry_is_exact_and_timezone_naive_rows_read_as_utc():
    assert is_expired_trial(_row("trialing", NOW), now=NOW) is False
    assert is_expired_trial(_row("trialing", NOW - timedelta(seconds=1)), now=NOW)
    naive = _row("trialing", PAST.replace(tzinfo=None))
    assert is_expired_trial(naive, now=NOW) is True
    assert trial_ended_at(naive) == PAST


def test_trial_ended_at_only_reports_trial_rows():
    assert trial_ended_at(_row("trialing", PAST)) == PAST
    assert trial_ended_at(_row("active", PAST)) is None


@pytest.mark.parametrize(
    "status, period_end, stale",
    [
        ("trialing", PAST, True),
        ("trialing", FUTURE, False),
        ("active", NOW - timedelta(days=8), True),
        ("active", NOW - timedelta(days=6), False),
        ("past_due", NOW - timedelta(days=30), True),
        ("canceled", PAST, False),
    ],
)
def test_stale_detection(status, period_end, stale):
    assert is_stale_subscription(_row(status, period_end), now=NOW) is stale


def test_active_status_set_excludes_dunning():
    row = _row("past_due", FUTURE)
    assert is_entitled_subscription(row, now=NOW, statuses=ENTITLED_STATUSES) is True
    assert is_entitled_subscription(row, now=NOW, statuses=ACTIVE_STATUSES) is False


def test_sql_clause_matches_the_in_memory_rule():
    clause = str(entitlement_clause(models.Subscription, now=NOW))
    assert "subscription.status IN" in clause
    assert "subscription.status !=" in clause
    assert "subscription.current_period_end >=" in clause


def _seed(db_session, account_id, *, status, period_end, stripe_id):
    if crud_plan.get(db_session, id="teams") is None:
        crud_plan.create(
            db_session,
            obj_in={
                "id": "teams",
                "name": "Teams",
                "price_monthly": 29.0,
                "price_annually": 290.0,
                "features": {"max_users": 10},
                "is_active": True,
                "is_custom": False,
            },
        )
    return crud_subscription.create(
        db_session,
        obj_in={
            "account_id": account_id,
            "plan_id": "teams",
            "stripe_subscription_id": stripe_id,
            "status": status,
            "current_period_start": period_end - timedelta(days=14),
            "current_period_end": period_end,
        },
    )


@pytest.mark.parametrize(
    "status, offset_days, entitled",
    [
        ("trialing", -50, False),
        ("trialing", 13, True),
        ("active", -50, True),
        ("past_due", -50, True),
        ("canceled", 13, False),
    ],
)
def test_entitled_subscription_query_applies_the_rule(
    db_session, test_user, status, offset_days, entitled
):
    now = datetime.now(timezone.utc)
    _seed(
        db_session,
        test_user.account_id,
        status=status,
        period_end=now + timedelta(days=offset_days),
        stripe_id=f"sub_{status}_{offset_days}",
    )
    found = billing.entitled_subscription(db_session, str(test_user.account_id))
    assert (found is not None) is entitled


@pytest.mark.parametrize(
    "status, offset_days, live",
    [
        ("trialing", -50, False),
        ("trialing", 13, True),
        ("active", -50, True),
        # past_due is entitled but not "live" for checkout/quota callers.
        ("past_due", 13, False),
    ],
)
def test_get_active_for_account_applies_the_rule(
    db_session, test_user, status, offset_days, live
):
    now = datetime.now(timezone.utc)
    _seed(
        db_session,
        test_user.account_id,
        status=status,
        period_end=now + timedelta(days=offset_days),
        stripe_id=f"sub_live_{status}_{offset_days}",
    )
    found = crud_subscription.get_active_for_account(
        db_session, account_id=str(test_user.account_id)
    )
    assert (found is not None) is live


def test_expired_trial_row_is_still_readable_and_untouched(db_session, test_user):
    """Falling back to Free must not delete or rewrite provider history."""
    now = datetime.now(timezone.utc)
    row = _seed(
        db_session,
        test_user.account_id,
        status="trialing",
        period_end=now - timedelta(days=50),
        stripe_id="sub_expired_history",
    )
    assert billing.entitled_subscription(db_session, str(test_user.account_id)) is None
    latest = crud_subscription.get_latest_for_account(
        db_session, account_id=str(test_user.account_id)
    )
    assert latest is not None
    assert str(latest.id) == str(row.id)
    assert latest.status == "trialing"

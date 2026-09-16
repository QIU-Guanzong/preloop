"""Subscription transitions preserve their longer history promises."""

from unittest.mock import MagicMock

import pytest

from preloop.models import models
from preloop.models.crud.history_policy import (
    HISTORY_RETENTION_KEY,
    analytics_plan_retention,
    analytics_read_window,
    preserve_history_retention,
)


@pytest.mark.parametrize(
    "previous, incoming, expected",
    [(None, 730, 730), (730, 183, 730), (365, 730, 730), (-1, 365, -1), (365, -1, -1)],
)
def test_history_floor_is_monotonic(previous, incoming, expected):
    account = models.Account(
        meta_data={"other": "preserved", HISTORY_RETENTION_KEY: previous}
    )
    db = MagicMock()
    db.execute.return_value.one_or_none.return_value = (dict(account.meta_data), None)
    db.get.return_value = account
    preserve_history_retention(db, account_id="account-a", days=incoming)
    assert account.meta_data[HISTORY_RETENTION_KEY] == expected
    assert account.subscription_history_retention_days == expected
    assert account.meta_data["other"] == "preserved"
    db.commit.assert_not_called()
    statement = str(db.execute.call_args.args[0])
    assert "account.id =" in statement
    assert "FOR UPDATE" in statement


@pytest.mark.parametrize("days", [None, 0, -2, True])
def test_absent_or_invalid_promise_never_changes_storage(days):
    db = MagicMock()
    preserve_history_retention(db, account_id="account-a", days=days)
    db.execute.assert_not_called()


def test_stale_separate_session_cannot_lower_committed_promise(db_engine):
    """A preloaded365-day identity must not overwrite a concurrent730-day promise."""
    from sqlalchemy import delete
    from sqlalchemy.orm import Session
    from uuid import uuid4

    account_id = uuid4()
    with Session(db_engine) as seed:
        seed.add(
            models.Account(
                id=account_id,
                organization_name="history-race",
                meta_data={HISTORY_RETENTION_KEY: 365, "unrelated": "original"},
            )
        )
        seed.commit()
    try:
        with Session(db_engine) as stale, Session(db_engine) as writer:
            old = stale.get(models.Account, account_id)
            assert old.meta_data[HISTORY_RETENTION_KEY] == 365
            preserve_history_retention(writer, account_id=account_id, days=730)
            writer.commit()
            # The stale caller has a legitimate pending change to another key.
            old.meta_data = {**old.meta_data, "caller_note": "keep"}
            preserve_history_retention(stale, account_id=account_id, days=365)
            stale.commit()
        with Session(db_engine) as check:
            stored = check.get(models.Account, account_id).meta_data
            assert stored[HISTORY_RETENTION_KEY] == 730
            assert stored["caller_note"] == "keep"
            assert stored["unrelated"] == "original"
    finally:
        with Session(db_engine) as cleanup:
            cleanup.execute(
                delete(models.Account).where(models.Account.id == account_id)
            )
            cleanup.commit()


@pytest.mark.parametrize("promise", [730, -1])
def test_unrelated_stale_metadata_write_cannot_erase_floor(db_engine, promise):
    """A writer that knows nothing about retention cannot lose the promise."""
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from uuid import uuid4

    from sqlalchemy import delete, select
    from sqlalchemy.orm import Session

    from preloop.services import analytics_history, retention_purge
    from preloop.services.retention_policy import CLASS_USAGE

    account_id, usage_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    with Session(db_engine) as seed:
        seed.add(
            models.Account(
                id=account_id,
                organization_name="whole-json-race",
                meta_data={HISTORY_RETENTION_KEY: 183},
            )
        )
        seed.flush()
        seed.add(
            models.ApiUsage(
                id=usage_id,
                account_id=account_id,
                endpoint="/test",
                method="POST",
                status_code=200,
                duration=0.1,
                timestamp=now - timedelta(days=500),
            )
        )
        seed.commit()
    try:
        with Session(db_engine) as stale, Session(db_engine) as writer:
            old = stale.get(models.Account, account_id)
            preserve_history_retention(writer, account_id=account_id, days=promise)
            writer.commit()
            old.meta_data = {"unrelated": "replacement"}
            stale.commit()
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                analytics_history,
                "get_plugin_manager",
                lambda: SimpleNamespace(get_service=lambda _: None),
            )
            with Session(db_engine) as check:
                account = check.get(models.Account, account_id)
                assert account.subscription_history_retention_days == promise
                assert account.meta_data == {"unrelated": "replacement"}
                result = retention_purge.purge_class(
                    check,
                    account=account,
                    record_class=CLASS_USAGE,
                    now=now,
                    batch_size=1,
                    max_batches=2,
                    dry_run=False,
                )
                assert result.deleted == 0
                assert (
                    check.execute(
                        select(models.ApiUsage.id).where(models.ApiUsage.id == usage_id)
                    ).scalar_one()
                    == usage_id
                )
    finally:
        with Session(db_engine) as cleanup:
            cleanup.execute(
                delete(models.ApiUsage).where(models.ApiUsage.account_id == account_id)
            )
            cleanup.execute(
                delete(models.Account).where(models.Account.id == account_id)
            )
            cleanup.commit()


@pytest.mark.parametrize("previous_floor,expected", [(None, 730), (900, 900), (-1, -1)])
def test_reconciliation_separates_audit_and_preserves_materialized_floor(
    db_session, test_user, previous_floor, expected
):
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4
    from preloop.models.crud.billing import billing

    account = db_session.get(models.Account, test_user.account_id)
    account.subscription_history_retention_days = previous_floor
    account.meta_data = {HISTORY_RETENTION_KEY: previous_floor}
    plan_id = "history-" + uuid4().hex
    db_session.add(
        models.Plan(
            id=plan_id,
            name=plan_id,
            features={"retention_days": 730, "audit_logs_retention_days": -1},
        )
    )
    db_session.flush()
    now = datetime.now(UTC)
    billing.reconcile_subscription(
        db_session,
        account_id=str(account.id),
        stripe_id="sub_" + uuid4().hex,
        values={
            "plan_id": plan_id,
            "status": "past_due",
            "current_period_start": now,
            "current_period_end": now + timedelta(days=30),
            "billing_state": {},
        },
    )
    assert account.subscription_history_retention_days == expected
    assert account.meta_data[HISTORY_RETENTION_KEY] == expected


@pytest.mark.parametrize(
    "features, expected",
    [
        # Stated window wins, and it may be shorter than what is stored.
        ({"retention_days": 183, "analytics_window_days": 90}, 90),
        ({"retention_days": 730, "analytics_window_days": -1}, -1),
        # No window stated: show everything the plan keeps.
        ({"retention_days": 730}, 730),
        ({"audit_logs_retention_days": 900}, 900),
        # Nonsense is ignored rather than allowed to blank the view.
        ({"retention_days": 365, "analytics_window_days": 0}, 365),
        ({"retention_days": 365, "analytics_window_days": -5}, 365),
        ({"retention_days": 365, "analytics_window_days": True}, 365),
        ({"retention_days": 365, "analytics_window_days": "90"}, 365),
        ({}, None),
        (None, None),
    ],
)
def test_read_window_defaults_to_what_the_plan_stores(features, expected):
    assert analytics_read_window(features) == expected


@pytest.mark.parametrize(
    "features",
    [
        {"retention_days": 183, "analytics_window_days": 90},
        {"retention_days": 730, "analytics_window_days": -1},
    ],
)
def test_the_read_window_is_invisible_to_the_purge_input(features):
    """Physical deletion follows retention_days alone, whatever is displayed."""
    assert analytics_plan_retention(features) == features["retention_days"]

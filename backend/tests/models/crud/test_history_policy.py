"""Subscription transitions preserve their longer history promises."""

from unittest.mock import MagicMock

import pytest

from preloop.models import models
from preloop.models.crud.history_policy import (
    HISTORY_RETENTION_KEY,
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

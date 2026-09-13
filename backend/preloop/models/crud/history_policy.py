"""Persist subscription history promises without shortening existing retention."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from preloop.models import models

HISTORY_RETENTION_KEY = "subscription_history_retention_days"


def preserve_history_retention(
    db: Session, *, account_id: Any, days: int | None
) -> None:
    """Raise the stored history floor in the caller's account transaction.

    Call before replacing subscription terms, for both old and new plans.
    ``-1`` preserves unlimited history. This never commits or lowers a promise.
    """
    if days is None or isinstance(days, bool) or (days != -1 and days <= 0):
        return
    # Select scalar metadata so an already-loaded Account cannot supply a stale
    # value after we acquire the lock. Do not autoflush a stale pending JSON
    # replacement before acquiring that lock.
    with db.no_autoflush:
        row = db.execute(
            select(
                models.Account.meta_data,
                models.Account.subscription_history_retention_days,
            )
            .where(models.Account.id == account_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise ValueError("Account not found")
        account = db.get(models.Account, account_id)
        meta = deepcopy(row[0] or {})
        history = inspect(account).attrs.meta_data.history
        if history.has_changes():
            # Preserve the caller's explicitly changed unrelated metadata keys;
            # do not copy stale unchanged keys back over concurrent updates.
            before = (history.deleted[0] if history.deleted else {}) or {}
            desired = account.meta_data or {}
            for key in before.keys() | desired.keys():
                if key == HISTORY_RETENTION_KEY or before.get(key) == desired.get(key):
                    continue
                if key in desired:
                    meta[key] = deepcopy(desired[key])
                else:
                    meta.pop(key, None)
    previous = longest_history_promise(row[1], meta.get(HISTORY_RETENTION_KEY))
    retained = longest_history_promise(previous, days)
    meta[HISTORY_RETENTION_KEY] = retained
    # Write the dedicated field even if the compatibility mirror was correct.
    account.subscription_history_retention_days = retained
    account.meta_data = meta
    db.add(account)
    db.flush()


def longest_history_promise(*values: Any) -> int:
    """Combine valid promises, with unlimited dominating every finite value."""
    valid = [value for value in values if type(value) is int and value >= -1]
    return -1 if -1 in valid else max([0, *valid])


def subscription_history_retention(db: Session, *, account_id: Any) -> int:
    """Read persisted plan promises for a standalone purge without EE hooks.

    Include historical subscription rows conservatively. A canceled contract
    must not silently shorten the retention already promised for its records.
    The monotonic account metadata also covers in-place plan replacements.
    """
    features = (
        db.execute(
            select(models.Plan.features)
            .join(models.Subscription, models.Subscription.plan_id == models.Plan.id)
            .where(models.Subscription.account_id == account_id)
        )
        .scalars()
        .all()
    )
    longest = 0
    for values in features:
        for key in ("retention_days", "audit_logs_retention_days"):
            days = (values or {}).get(key)
            if days == -1:
                return -1
            if isinstance(days, int) and not isinstance(days, bool) and days > 0:
                longest = max(longest, days)
    return longest


def lock_account_for_retention(
    db: Session, *, account_id: Any
) -> models.Account | None:
    """Read fresh account policy under a bounded lock for one purge transaction.

    The purge owns its session and enters with no pending writes. Skip accounts
    being changed rather than waiting on a billing/configuration transaction.
    Account-first ordering matches billing reconciliation and promise updates.
    """
    with db.no_autoflush:
        return db.execute(
            select(models.Account)
            .where(models.Account.id == account_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

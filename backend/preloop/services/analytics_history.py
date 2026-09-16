"""Plan history access for reporting, separate from stored audit retention.

Only report/read paths call this module. Gateway execution, authorization,
firewall, approvals, budgets and audit/evidence exports never use this cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.history_policy import (
    HISTORY_RETENTION_KEY,
    longest_history_promise,
    subscription_history_retention,
)
from preloop.plugins import get_plugin_manager


#: Sanity bound on a plan policy's answer, in days. This is not a plan value
#: and not a retention floor: it only rejects an obviously broken policy
#: (zero, negative, a boolean) before it can shrink a window. The shortest
#: window any plan may legitimately show is the entry plan's, so this sits at
#: 90 days. Physical retention floors live in retention_policy, untouched.
MINIMUM_ANALYTICS_POLICY_DAYS = 90


def _policy_days(db: Session, *, account: models.Account, provider: Any) -> int | None:
    """Validate one plan policy answer; a broken policy never shortens anything."""
    if provider is None:
        return None
    try:
        days = provider(db, account=account)
    except Exception as exc:
        raise HTTPException(
            503, "Analytics history policy is temporarily unavailable"
        ) from exc
    if days is None or days == -1:
        return days
    if (
        isinstance(days, bool)
        or not isinstance(days, int)
        or days < MINIMUM_ANALYTICS_POLICY_DAYS
    ):
        raise HTTPException(503, "Analytics history policy is invalid")
    return days


def _configured_history_days(db: Session, *, account: models.Account) -> int | None:
    """Resolve a cloud reporting window; OSS/self-host has no cloud cutoff.

    A broken billing policy cannot choose a shorter window or authorize a
    purge. Fail explicitly on reporting paths; purge also fails before delete.
    """
    return _policy_days(
        db,
        account=account,
        provider=get_plugin_manager().get_service("analytics_history_policy"),
    )


def _configured_storage_days(db: Session, *, account: models.Account) -> int | None:
    """Resolve what a plan KEEPS, which can be more than reporting shows.

    A plan may display a shorter window than it stores. Deleting to the
    displayed window would make that window permanent, so the purge path asks
    for the storage promise. Plugin builds that publish only the reporting
    policy keep their old behaviour, where the two were the same number.
    """
    provider = get_plugin_manager().get_service("analytics_storage_policy")
    if provider is None:
        return _configured_history_days(db, account=account)
    return _policy_days(db, account=account, provider=provider)


def analytics_history_days(db: Session, *, account: models.Account) -> int | None:
    """Reporting has no cutoff for either no cloud policy or explicit unlimited."""
    days = _configured_history_days(db, account=account)
    return None if days == -1 else days


def history_cutoff(
    db: Session, *, account: models.Account, now: datetime | None = None
) -> datetime | None:
    """Earliest visible reporting timestamp, independent of a requested end."""
    days = analytics_history_days(db, account=account)
    return None if days is None else (now or datetime.now(UTC)) - timedelta(days=days)


@dataclass(frozen=True)
class AnalyticsHistoryWindow:
    """One reporting request's policy snapshot; never cached on Account/Session."""

    cutoff: datetime | None


def resolve_history_window(
    db: Session, *, account: models.Account, now: datetime | None = None
) -> AnalyticsHistoryWindow:
    """Resolve once, then pass this snapshot to every read in the request."""
    return AnalyticsHistoryWindow(history_cutoff(db, account=account, now=now))


def restrict_history_window(
    db: Session,
    *,
    account: models.Account,
    start_date: datetime | None,
    end_date: datetime | None,
    now: datetime | None = None,
    history_window: AnalyticsHistoryWindow | None = None,
) -> tuple[datetime | None, datetime | None]:
    """Clamp analytics windows; refuse wholly unavailable historical periods.

    Returning an empty/zero result for a wholly unavailable period would
    falsely imply observed zero usage. Upgrades cannot recreate deleted rows.
    """
    cutoff = (
        history_window.cutoff
        if history_window is not None
        else history_cutoff(db, account=account, now=now)
    )
    if cutoff is None:
        return start_date, end_date
    start = (
        start_date.replace(tzinfo=UTC)
        if start_date and start_date.tzinfo is None
        else start_date
    )
    end = (
        end_date.replace(tzinfo=UTC)
        if end_date and end_date.tzinfo is None
        else end_date
    )
    if end is not None and end <= cutoff:
        raise HTTPException(
            403,
            detail={
                "code": "analytics_history_unavailable",
                "available_from": cutoff.isoformat(),
                "message": "This period is outside your plan's analytics history. Stored audit and evidence records follow their own retention policy. Upgrading cannot restore records already deleted.",
            },
        )
    return max(start, cutoff) if start else cutoff, end


def storage_history_days(db: Session, *, account: models.Account) -> int | None:
    """Minimum physical history floor, preserving earlier longer promises.

    ``None`` denotes no finite retention limit when a cloud plan explicitly
    supplies unlimited history. No plugin means normal deployment retention.
    """
    stored = longest_history_promise(
        account.subscription_history_retention_days,
        (account.meta_data or {}).get(HISTORY_RETENTION_KEY),
    )
    if stored == -1:
        return None
    current = _configured_storage_days(db, account=account)
    # Reporting may legitimately fall back to Free after entitlement ends.
    # Existing subscriptions can predate Account's durable floor and must
    # still protect physical rows even while the billing plugin is enabled.
    persisted = subscription_history_retention(db, account_id=account.id)
    promised = longest_history_promise(stored, current, persisted)
    return None if promised == -1 else promised


def require_session_history(
    db: Session,
    *,
    account: models.Account,
    summary: dict[str, Any],
    history_window: AnalyticsHistoryWindow | None = None,
) -> None:
    """Refuse old analytics while allowing a long-lived session with new activity."""
    cutoff = (
        history_window.cutoff
        if history_window is not None
        else history_cutoff(db, account=account)
    )
    if cutoff is None:
        return
    dates = [
        value.replace(tzinfo=UTC) if value.tzinfo is None else value
        for key in ("started_at", "last_activity_at", "last_request_at", "ended_at")
        if isinstance(value := summary.get(key), datetime)
    ]
    if dates and max(dates) < cutoff:
        raise HTTPException(
            403,
            detail={
                "code": "analytics_history_unavailable",
                "available_from": cutoff.isoformat(),
                "message": "This session has no activity within your plan's analytics history. Session controls and stored audit evidence remain governed separately.",
            },
        )

"""One definition of "this subscription entitles the account".

Every entitlement path (shared CRUD lookups, the enterprise billing plugin's
premium gate, seat and agent capacity, hosted-model caps, plan comparison and
checkout) resolves entitlement through this module. Nothing may re-state the
rule locally: a second copy is how an account whose trial ended kept a paid
plan's limits for weeks.

The rule
--------
A subscription row entitles its account when both hold:

1. ``status`` is in the caller's accepted status set, and
2. the row is not an *expired trial*, i.e. NOT (``status == "trialing"`` and
   ``current_period_end`` is already in the past).

Status alone is not enough. Stripe moves a finished trial to ``canceled`` or
``paused``, but that transition only reaches us through a webhook. A missed
webhook leaves ``status == "trialing"`` on the row forever, and a status-only
check keeps handing out a paid plan nobody is paying for. The stored period
end is provider data we already persist, so honouring it costs nothing and
closes the hole even when the webhook never arrives.

``past_due`` is deliberately still entitled where a caller accepts it: Stripe
is retrying the card and the customer intends to pay. Cutting access during
dunning turns a billing hiccup into churn. Only ``trialing`` carries the date
check, because only a trial has a hard provider-side expiry that grants free
access until it passes.

Two status sets exist on purpose:

``ENTITLED_STATUSES``
    ``active``, ``trialing``, ``past_due``. Premium feature access and every
    plan-terms lookup.
``ACTIVE_STATUSES``
    ``active``, ``trialing``. Checkout de-duplication, the trial hosted-model
    cap and ingestion quota, which must not treat a dunning subscription as a
    live trial.

Both run through the same expiry rule, so neither can drift.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import ColumnElement, or_

# Statuses that grant premium access, including Stripe's dunning window.
ENTITLED_STATUSES: tuple[str, ...] = ("active", "trialing", "past_due")

# Statuses that mean "a live subscription exists right now", excluding dunning.
ACTIVE_STATUSES: tuple[str, ...] = ("active", "trialing")

TRIALING_STATUS = "trialing"


def _as_utc(value: Any) -> Optional[datetime]:
    """Normalise a stored timestamp to an aware UTC datetime.

    Args:
        value: A datetime from the ORM, aware or naive.

    Returns:
        The aware UTC equivalent, or None when the value is not a datetime.
    """
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def trial_ended_at(subscription: Any) -> Optional[datetime]:
    """The instant a trial subscription's trial period ended.

    Args:
        subscription: A Subscription row, or None.

    Returns:
        The aware UTC ``current_period_end`` when the row's status is
        ``trialing``, otherwise None. Callers that want "has it ended?"
        should use :func:`is_expired_trial`, which compares against now.
    """
    if subscription is None or getattr(subscription, "status", None) != TRIALING_STATUS:
        return None
    return _as_utc(getattr(subscription, "current_period_end", None))


def is_expired_trial(subscription: Any, *, now: Optional[datetime] = None) -> bool:
    """Whether this row is a trial whose provider-side end has passed.

    Args:
        subscription: A Subscription row, or None.
        now: Comparison instant, defaulting to the current UTC time.

    Returns:
        True only for a ``trialing`` row whose ``current_period_end`` is in
        the past. Never True for ``active``, ``past_due`` or any terminal
        status: a paid subscription that has not been reconciled since its
        period rolled over is still a paying customer.
    """
    ended_at = trial_ended_at(subscription)
    if ended_at is None:
        return False
    return ended_at < (_as_utc(now) or datetime.now(timezone.utc))


def is_live_trial(subscription: Any, *, now: Optional[datetime] = None) -> bool:
    """Whether this row is a trial that is still running.

    Args:
        subscription: A Subscription row, or None.
        now: Comparison instant, defaulting to the current UTC time.

    Returns:
        True only for a ``trialing`` row whose end has not passed. This is the
        condition for applying trial-only limits such as the hosted-model hard
        cap: an ended trial must not keep a cap that implies free compute.
    """
    if subscription is None:
        return False
    if getattr(subscription, "status", None) != TRIALING_STATUS:
        return False
    return not is_expired_trial(subscription, now=now)


def is_entitled_subscription(
    subscription: Any,
    *,
    now: Optional[datetime] = None,
    statuses: Sequence[str] = ENTITLED_STATUSES,
) -> bool:
    """Whether a subscription row entitles its account.

    This is the single in-memory form of the rule. Query paths use
    :func:`entitlement_clause` so the database applies the same predicate.

    Args:
        subscription: A Subscription row, or None.
        now: Comparison instant, defaulting to the current UTC time.
        statuses: Accepted statuses, defaulting to :data:`ENTITLED_STATUSES`.

    Returns:
        True when the status is accepted and the row is not an expired trial.
    """
    if subscription is None:
        return False
    if getattr(subscription, "status", None) not in tuple(statuses):
        return False
    return not is_expired_trial(subscription, now=now)


def entitlement_clause(
    subscription_model: Any,
    *,
    now: Optional[datetime] = None,
    statuses: Iterable[str] = ENTITLED_STATUSES,
) -> ColumnElement[bool]:
    """SQL form of :func:`is_entitled_subscription`, for use in a filter.

    Args:
        subscription_model: The Subscription mapped class.
        now: Comparison instant, defaulting to the current UTC time.
        statuses: Accepted statuses, defaulting to :data:`ENTITLED_STATUSES`.

    Returns:
        A boolean clause selecting rows the account is entitled to. Emitted
        as one clause so callers keep a single ``.filter()`` call.
    """
    moment = _as_utc(now) or datetime.now(timezone.utc)
    return subscription_model.status.in_(tuple(statuses)) & or_(
        subscription_model.status != TRIALING_STATUS,
        subscription_model.current_period_end >= moment,
    )


def is_stale_subscription(
    subscription: Any,
    *,
    now: Optional[datetime] = None,
    grace_days: int = 7,
) -> bool:
    """Whether a row's stored state is old enough to distrust.

    Used by read paths that repair provider state opportunistically: an
    expired trial, or any nominally entitled row whose billing period ended
    more than ``grace_days`` ago, means we have almost certainly missed a
    provider webhook.

    Args:
        subscription: A Subscription row, or None.
        now: Comparison instant, defaulting to the current UTC time.
        grace_days: How far past its period end an entitled row may sit
            before it counts as stale.

    Returns:
        True when the row should be re-read from the provider.
    """
    if subscription is None:
        return False
    moment = _as_utc(now) or datetime.now(timezone.utc)
    if is_expired_trial(subscription, now=moment):
        return True
    if getattr(subscription, "status", None) not in ENTITLED_STATUSES:
        return False
    period_end = _as_utc(getattr(subscription, "current_period_end", None))
    if period_end is None:
        return False
    return (moment - period_end).days > grace_days


__all__ = [
    "ACTIVE_STATUSES",
    "ENTITLED_STATUSES",
    "TRIALING_STATUS",
    "entitlement_clause",
    "is_entitled_subscription",
    "is_expired_trial",
    "is_live_trial",
    "is_stale_subscription",
    "trial_ended_at",
]

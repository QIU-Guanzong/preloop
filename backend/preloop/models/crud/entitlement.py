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

Withdrawn plans and grandfathering
----------------------------------
A plan row with ``is_active == False`` has been withdrawn from sale. Nobody
can buy it, it is hidden from the plan list, and the only reason the row still
exists is that live subscriptions resolve their terms through it. Keeping such
a plan is a *grandfathering* promise, and a promise made to paying customers:
:func:`grandfathers_withdrawn_plan` requires a provider-backed paid row
(``active`` or ``past_due`` with a ``stripe_subscription_id``) before a
withdrawn plan's terms are handed out.

Status alone is not enough here either, and for a different reason than
above. An *ended* trial of a plan that has since been withdrawn is not a
grandfathered customer: it is someone who was evaluating a product we no
longer sell. Letting that row keep resolving the withdrawn plan hands out
its terms and its *name* to an account that never paid for it, and that is
what left a staging account on the withdrawn "teams" plan instead of Free
after its legacy per-seat trial ended. A trial that is still *running* is
kept, because it is a promise already made and it expires by itself.

Grandfathering is therefore two independent tests, both of which must hold:
the subscription entitles (:func:`is_entitled_subscription`) and, when the
plan is withdrawn, somebody is paying for it or the trial has not ended yet
(:func:`grandfathers_withdrawn_plan`).

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

from sqlalchemy import ColumnElement, or_, select

# Statuses that grant premium access, including Stripe's dunning window.
ENTITLED_STATUSES: tuple[str, ...] = ("active", "trialing", "past_due")

# Statuses that mean "a live subscription exists right now", excluding dunning.
ACTIVE_STATUSES: tuple[str, ...] = ("active", "trialing")

# Statuses that mean money is actually moving for this subscription. A trial
# is not among them: it is free by construction. These are the only statuses
# that grandfather an account onto a plan that is no longer sold.
PAID_STATUSES: tuple[str, ...] = ("active", "past_due")

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


def is_paid_subscription(subscription: Any) -> bool:
    """Whether the provider is charging for this subscription right now.

    Args:
        subscription: A Subscription row, or None.

    Returns:
        True for an ``active`` or ``past_due`` row that carries a provider
        subscription id. A trial is never paid, however far in the future its
        period end sits, and a row with no ``stripe_subscription_id`` is a
        local artefact (a seeded row, or a plan change that was written
        locally and never completed at the provider) rather than evidence of
        a paying customer.
    """
    if subscription is None:
        return False
    if getattr(subscription, "status", None) not in PAID_STATUSES:
        return False
    provider_id = getattr(subscription, "stripe_subscription_id", None)
    return bool(str(provider_id or "").strip())


def grandfathers_withdrawn_plan(
    subscription: Any, *, plan: Any, now: Optional[datetime] = None
) -> bool:
    """Whether this subscription may keep resolving a withdrawn plan.

    Applied on top of :func:`is_entitled_subscription`, never instead of it.

    A trial that is still running is the one unpaid case that keeps the
    withdrawn plan. It is a promise already made, it cannot be renewed (a
    withdrawn plan is not purchasable, so no new trial can start on one), and
    it ends by itself on a date the provider set, after which the expiry rule
    resolves the account to the default plan with no operator action. An
    *ended* trial is the opposite: nothing bounds it, which is how one sat at
    ``trialing`` for seven weeks and kept a retired plan's terms and name.

    Args:
        subscription: A Subscription row, or None.
        plan: The Plan row the subscription points at, or None when it cannot
            be loaded. An unknown plan is treated as withdrawn: resolving the
            terms of a row we cannot see is exactly the case that should fall
            back to the default plan.
        now: Comparison instant, defaulting to the current UTC time.

    Returns:
        True when the plan is still on sale (grandfathering does not apply and
        ordinary entitlement decides alone), when the plan is withdrawn and
        somebody is paying for it, or while a trial of it is still running.
    """
    if plan is not None and bool(getattr(plan, "is_active", False)):
        return True
    if is_live_trial(subscription, now=now):
        return True
    return is_paid_subscription(subscription)


def grandfather_clause(
    subscription_model: Any, plan_model: Any, *, now: Optional[datetime] = None
) -> ColumnElement[bool]:
    """SQL form of :func:`grandfathers_withdrawn_plan`, for use in a filter.

    Emitted as a single clause over the subscription table, with the plan
    lookup as a scalar subquery rather than a join. Callers therefore keep
    their existing query shape and add one more ``.filter()`` argument: a
    join would change the row shape of every caller, and the plan table is
    the size of the price list.

    Args:
        subscription_model: The Subscription mapped class or alias.
        plan_model: The Plan mapped class, whose ``id`` and ``is_active``
            columns say which plans are still sold.
        now: Comparison instant, defaulting to the current UTC time.

    Returns:
        A boolean clause that passes plans still on sale, and withdrawn plans
        only for a provider-backed paid subscription or a trial that is still
        running. A subscription whose plan row does not exist at all matches
        no plan still on sale, so it too needs one of those, matching
        :func:`grandfathers_withdrawn_plan` with ``plan=None``.
    """
    moment = _as_utc(now) or datetime.now(timezone.utc)
    plans_on_sale = select(plan_model.id).where(plan_model.is_active.is_(True))
    return or_(
        subscription_model.plan_id.in_(plans_on_sale),
        subscription_model.status.in_(PAID_STATUSES)
        & subscription_model.stripe_subscription_id.isnot(None),
        (subscription_model.status == TRIALING_STATUS)
        & (subscription_model.current_period_end >= moment),
    )


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
    "PAID_STATUSES",
    "TRIALING_STATUS",
    "entitlement_clause",
    "grandfather_clause",
    "grandfathers_withdrawn_plan",
    "is_entitled_subscription",
    "is_paid_subscription",
    "is_expired_trial",
    "is_live_trial",
    "is_stale_subscription",
    "trial_ended_at",
]

"""CRUD operations for Plan and Subscription models."""

from datetime import date
from typing import List, Optional

from sqlalchemy.orm import Session

from ..models.plan import Plan, Subscription, MonthlyUsage
from .base import CRUDBase
from .entitlement import ACTIVE_STATUSES, entitlement_clause


class CRUDPlan(CRUDBase[Plan]):
    """CRUD operations for Plan model."""

    def get_active_public_plans(self, db: Session) -> List[Plan]:
        """Get all active public plans."""
        return (
            db.query(Plan)
            .filter(Plan.is_active, Plan.is_custom.is_(False))
            .order_by(Plan.created_at.asc())
            .all()
        )

    def get_active_custom_plans_for_account(
        self, db: Session, *, account_id: str
    ) -> List[Plan]:
        """Get all active custom plans for a specific account."""
        return (
            db.query(Plan)
            .filter(
                Plan.is_active,
                Plan.is_custom,
                Plan.account_id == account_id,
            )
            .all()
        )


class CRUDSubscription(CRUDBase[Subscription]):
    """CRUD operations for Subscription model."""

    def get_latest_for_account(
        self, db: Session, *, account_id: str
    ) -> Optional[Subscription]:
        """Get the latest subscription for an account."""
        return (
            db.query(Subscription)
            .filter(Subscription.account_id == account_id)
            .order_by(Subscription.created_at.desc())
            .first()
        )

    def get_active_for_account(
        self, db: Session, *, account_id: str
    ) -> Optional[Subscription]:
        """Get the live subscription for an account, if it still is one.

        ``active`` or ``trialing`` only (never ``past_due``): callers are
        checkout de-duplication, the trial hosted-model cap and the ingestion
        quota, none of which should treat a dunning subscription as a live
        trial. A trial whose ``current_period_end`` has passed is not live and
        returns None, so those callers fall back to Free. The expiry rule is
        shared with every other entitlement lookup; see
        :mod:`preloop.models.crud.entitlement`.
        """
        return (
            db.query(Subscription)
            .filter(
                Subscription.account_id == account_id,
                entitlement_clause(Subscription, statuses=ACTIVE_STATUSES),
            )
            .order_by(Subscription.created_at.desc())
            .first()
        )

    def get_by_stripe_subscription_id(
        self, db: Session, *, stripe_subscription_id: str
    ) -> Optional[Subscription]:
        """Get subscription by Stripe subscription ID."""
        return (
            db.query(Subscription)
            .filter(Subscription.stripe_subscription_id == stripe_subscription_id)
            .first()
        )


class CRUDMonthlyUsage(CRUDBase[MonthlyUsage]):
    """CRUD operations for MonthlyUsage model."""

    def get_for_current_cycle(
        self, db: Session, *, subscription_id: str, today: date
    ) -> Optional[MonthlyUsage]:
        """Get usage record for the current billing cycle."""
        return (
            db.query(MonthlyUsage)
            .filter(
                MonthlyUsage.subscription_id == subscription_id,
                MonthlyUsage.billing_cycle_start <= today,
                MonthlyUsage.billing_cycle_end >= today,
            )
            .first()
        )


plan = CRUDPlan(Plan)
subscription = CRUDSubscription(Subscription)
monthly_usage = CRUDMonthlyUsage(MonthlyUsage)

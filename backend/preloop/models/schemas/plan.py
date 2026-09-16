"""
Pydantic schemas for Plans, Subscriptions, and Usage.
"""

from datetime import datetime, date
from typing import Dict, Any, Optional
import uuid

from pydantic import BaseModel, Field, ConfigDict


class PlanFeatures(BaseModel):
    """Defines the features and limits for a subscription plan."""

    api_calls_monthly: int = Field(..., description="Monthly API call limit.")
    ai_calls_monthly: int = Field(..., description="Monthly AI model call limit.")
    issues_ingested_monthly: int = Field(
        ..., description="Monthly issue/comment ingestion limit."
    )
    custom_ai_models_enabled: bool = Field(
        ..., description="Whether custom AI models are enabled."
    )
    custom_compliance_metrics_enabled: bool = Field(
        ..., description="Whether custom compliance metrics are enabled."
    )
    hosted_models_monthly_limit_usd: Optional[float] = Field(
        None,
        description="Included monthly spend allowance for Preloop-hosted built-in models.",
    )
    extra_credit_price_per_usd: Optional[float] = Field(
        None,
        description="Customer-facing price for each additional USD of hosted-model usage.",
    )
    analytics_window_days: Optional[int] = Field(
        None,
        description=(
            "Days of analytics history the plan shows at read time, -1 for "
            "unlimited. Never a storage promise: physical retention follows "
            "retention_days and is unaffected by this key."
        ),
    )


class PlanBase(BaseModel):
    """Base schema for a Plan."""

    name: str = Field(..., description="Name of the plan.")
    price_monthly: Optional[float] = Field(
        None, description="Monthly price of the plan, if available."
    )
    price_annually: Optional[float] = Field(
        None, description="Annual price of the plan, if available."
    )
    is_active: bool = Field(True, description="Whether the plan is currently active.")
    features: PlanFeatures = Field(..., description="Features and limits of the plan.")


class PlanCreate(PlanBase):
    """Schema for creating a new Plan."""

    pass


class Plan(PlanBase):
    """Schema for a Plan retrieved from the database."""

    id: str = Field(..., description="Primary key.")
    created_at: datetime = Field(..., description="Timestamp of creation.")
    updated_at: datetime = Field(..., description="Timestamp of last update.")

    model_config = ConfigDict(from_attributes=True)


class SubscriptionBase(BaseModel):
    """Base schema for a Subscription."""

    account_id: uuid.UUID = Field(..., description="Foreign key to Account.")
    plan_id: str = Field(..., description="Foreign key to Plan.")
    status: str = Field(
        "active", description="Subscription status (e.g., active, trialing, canceled)."
    )
    current_period_start: datetime = Field(
        ..., description="Start of the current billing period."
    )
    current_period_end: datetime = Field(
        ..., description="End of the current billing period."
    )
    stripe_subscription_id: Optional[str] = Field(
        None, description="ID of the subscription in Stripe."
    )


class SubscriptionCreate(SubscriptionBase):
    """Schema for creating a new Subscription."""

    pass


class Subscription(SubscriptionBase):
    """Schema for a Subscription retrieved from the database."""

    id: uuid.UUID = Field(..., description="Primary key.")
    created_at: datetime = Field(..., description="Timestamp of creation.")
    updated_at: datetime = Field(..., description="Timestamp of last update.")

    model_config = ConfigDict(from_attributes=True)


class MonthlyUsageBase(BaseModel):
    """Base schema for Monthly Usage."""

    subscription_id: uuid.UUID = Field(..., description="Foreign key to Subscriptions.")
    billing_cycle_start: date = Field(
        ..., description="Start date of the billing cycle."
    )
    billing_cycle_end: date = Field(..., description="End date of the billing cycle.")
    usage_counts: Dict[str, Any] = Field(
        {}, description="Aggregated usage counts for the cycle."
    )


class MonthlyUsageCreate(MonthlyUsageBase):
    """Schema for creating a new Monthly Usage record."""

    pass


class MonthlyUsage(MonthlyUsageBase):
    """Schema for a Monthly Usage record retrieved from the database."""

    id: uuid.UUID = Field(..., description="Primary key.")
    created_at: datetime = Field(..., description="Timestamp of creation.")
    updated_at: datetime = Field(..., description="Timestamp of last update.")

    model_config = ConfigDict(from_attributes=True)

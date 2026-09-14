"""
SQLAlchemy models for Plans, Subscriptions, and Usage.
"""

from sqlalchemy import (
    Column,
    String,
    Boolean,
    Float,
    ForeignKey,
    DateTime,
    Date,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .base import Base


class Plan(Base):
    """A subscription plan."""

    __tablename__ = "plan"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    price_monthly = Column(Float, nullable=True)
    price_annually = Column(Float, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    features = Column(JSONB, nullable=False)
    stripe_product_id = Column(String, nullable=True, unique=True)
    account_id = Column(
        UUID(as_uuid=True), ForeignKey("account.id"), nullable=True, index=True
    )
    is_custom = Column(Boolean, default=False, nullable=False)

    subscription = relationship("Subscription", back_populates="plan")


class Subscription(Base):
    """A subscription linking an organization to a plan."""

    __tablename__ = "subscription"

    id = Column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    account_id = Column(
        UUID(as_uuid=True), ForeignKey("account.id"), nullable=False, index=True
    )
    plan_id = Column(String, ForeignKey("plan.id"), nullable=False, index=True)
    status = Column(String, nullable=False, default="active")
    current_period_start = Column(DateTime(timezone=True), nullable=False)
    current_period_end = Column(DateTime(timezone=True), nullable=False)
    stripe_subscription_id = Column(String, nullable=True, unique=True)
    billing_state = Column(JSONB, nullable=False, server_default="{}", default=dict)

    account = relationship("Account", back_populates="subscriptions")
    plan = relationship("Plan", back_populates="subscription")
    monthly_usage = relationship(
        "MonthlyUsage", back_populates="subscription", cascade="all, delete-orphan"
    )


class MonthlyUsage(Base):
    """Aggregated monthly usage for a subscription."""

    __tablename__ = "monthly_usage"

    id = Column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    subscription_id = Column(
        UUID(as_uuid=True), ForeignKey("subscription.id"), nullable=False, index=True
    )
    billing_cycle_start = Column(Date, nullable=False)
    billing_cycle_end = Column(Date, nullable=False)
    usage_counts = Column(JSONB, nullable=False, server_default="{}")

    subscription = relationship("Subscription", back_populates="monthly_usage")

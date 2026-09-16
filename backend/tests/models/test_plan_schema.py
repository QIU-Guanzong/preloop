"""Tests for plan and subscription Pydantic schemas."""

from datetime import datetime, date
from uuid import uuid4

import pytest
from pydantic import ValidationError

from preloop.models.schemas.plan import (
    MonthlyUsage,
    MonthlyUsageBase,
    MonthlyUsageCreate,
    Plan,
    PlanBase,
    PlanCreate,
    PlanFeatures,
    Subscription,
    SubscriptionBase,
    SubscriptionCreate,
)


class TestPlanFeatures:
    """Test PlanFeatures schema."""

    def test_create_with_all_fields(self):
        """Test creating PlanFeatures with all fields."""
        features = PlanFeatures(
            api_calls_monthly=10000,
            ai_calls_monthly=1000,
            issues_ingested_monthly=5000,
            custom_ai_models_enabled=True,
            custom_compliance_metrics_enabled=True,
        )

        assert features.api_calls_monthly == 10000
        assert features.ai_calls_monthly == 1000
        assert features.issues_ingested_monthly == 5000
        assert features.custom_ai_models_enabled is True
        assert features.custom_compliance_metrics_enabled is True

    def test_analytics_window_is_optional_and_defaults_to_storage(self):
        """A plan that states no window shows everything it stores."""
        features = PlanFeatures(
            api_calls_monthly=1,
            ai_calls_monthly=1,
            issues_ingested_monthly=1,
            custom_ai_models_enabled=False,
            custom_compliance_metrics_enabled=False,
        )

        assert features.analytics_window_days is None
        assert (
            PlanFeatures(
                api_calls_monthly=1,
                ai_calls_monthly=1,
                issues_ingested_monthly=1,
                custom_ai_models_enabled=False,
                custom_compliance_metrics_enabled=False,
                analytics_window_days=90,
            ).analytics_window_days
            == 90
        )

    def test_required_fields(self):
        """Test that all fields are required."""
        with pytest.raises(ValidationError):
            PlanFeatures()

        with pytest.raises(ValidationError):
            PlanFeatures(api_calls_monthly=1000)


class TestPlanBase:
    """Test PlanBase schema."""

    def test_create_with_required_fields(self):
        """Test creating PlanBase with required fields."""
        features = PlanFeatures(
            api_calls_monthly=10000,
            ai_calls_monthly=1000,
            issues_ingested_monthly=5000,
            custom_ai_models_enabled=True,
            custom_compliance_metrics_enabled=False,
        )

        plan = PlanBase(name="Free Plan", features=features)

        assert plan.name == "Free Plan"
        assert plan.price_monthly is None
        assert plan.price_annually is None
        assert plan.is_active is True
        assert plan.features == features

    def test_create_with_all_fields(self):
        """Test creating PlanBase with all fields."""
        features = PlanFeatures(
            api_calls_monthly=50000,
            ai_calls_monthly=5000,
            issues_ingested_monthly=20000,
            custom_ai_models_enabled=True,
            custom_compliance_metrics_enabled=True,
        )

        plan = PlanBase(
            name="Pro Plan",
            price_monthly=99.99,
            price_annually=999.00,
            is_active=True,
            features=features,
        )

        assert plan.name == "Pro Plan"
        assert plan.price_monthly == 99.99
        assert plan.price_annually == 999.00
        assert plan.is_active is True


class TestPlanCreate:
    """Test PlanCreate schema."""

    def test_inherits_from_base(self):
        """Test that PlanCreate inherits from PlanBase."""
        features = PlanFeatures(
            api_calls_monthly=10000,
            ai_calls_monthly=1000,
            issues_ingested_monthly=5000,
            custom_ai_models_enabled=False,
            custom_compliance_metrics_enabled=False,
        )

        plan = PlanCreate(name="Starter Plan", features=features)

        assert isinstance(plan, PlanBase)
        assert plan.name == "Starter Plan"


class TestPlan:
    """Test Plan schema."""

    def test_create_complete_plan(self):
        """Test creating complete Plan with all fields."""
        features = PlanFeatures(
            api_calls_monthly=10000,
            ai_calls_monthly=1000,
            issues_ingested_monthly=5000,
            custom_ai_models_enabled=True,
            custom_compliance_metrics_enabled=False,
        )

        plan_id = "plan_" + str(uuid4())[:8]
        created_at = datetime.now()
        updated_at = datetime.now()

        plan = Plan(
            id=plan_id,
            name="Enterprise Plan",
            price_monthly=299.99,
            price_annually=2999.00,
            is_active=True,
            features=features,
            created_at=created_at,
            updated_at=updated_at,
        )

        assert plan.id == plan_id
        assert plan.name == "Enterprise Plan"
        assert plan.price_monthly == 299.99
        assert plan.created_at == created_at
        assert plan.updated_at == updated_at

    def test_from_attributes_config(self):
        """Test that from_attributes is enabled in Config."""
        assert Plan.model_config.get("from_attributes") is True


class TestSubscriptionBase:
    """Test SubscriptionBase schema."""

    def test_create_with_required_fields(self):
        """Test creating SubscriptionBase with required fields."""
        account_id = uuid4()
        plan_id = "plan_free"
        start = datetime.now()
        end = datetime(2025, 12, 31, 23, 59, 59)

        sub = SubscriptionBase(
            account_id=account_id,
            plan_id=plan_id,
            current_period_start=start,
            current_period_end=end,
        )

        assert sub.account_id == account_id
        assert sub.plan_id == plan_id
        assert sub.status == "active"  # default value
        assert sub.current_period_start == start
        assert sub.current_period_end == end
        assert sub.stripe_subscription_id is None

    def test_create_with_all_fields(self):
        """Test creating SubscriptionBase with all fields."""
        account_id = uuid4()
        plan_id = "plan_pro"
        start = datetime.now()
        end = datetime(2025, 12, 31, 23, 59, 59)

        sub = SubscriptionBase(
            account_id=account_id,
            plan_id=plan_id,
            status="trialing",
            current_period_start=start,
            current_period_end=end,
            stripe_subscription_id="sub_1234567890",
        )

        assert sub.status == "trialing"
        assert sub.stripe_subscription_id == "sub_1234567890"


class TestSubscriptionCreate:
    """Test SubscriptionCreate schema."""

    def test_inherits_from_base(self):
        """Test that SubscriptionCreate inherits from SubscriptionBase."""
        account_id = uuid4()
        plan_id = "plan_starter"
        start = datetime.now()
        end = datetime(2025, 12, 31, 23, 59, 59)

        sub = SubscriptionCreate(
            account_id=account_id,
            plan_id=plan_id,
            current_period_start=start,
            current_period_end=end,
        )

        assert isinstance(sub, SubscriptionBase)
        assert sub.account_id == account_id


class TestSubscription:
    """Test Subscription schema."""

    def test_create_complete_subscription(self):
        """Test creating complete Subscription with all fields."""
        sub_id = uuid4()
        account_id = uuid4()
        plan_id = "plan_enterprise"
        start = datetime.now()
        end = datetime(2025, 12, 31, 23, 59, 59)
        created_at = datetime.now()
        updated_at = datetime.now()

        sub = Subscription(
            id=sub_id,
            account_id=account_id,
            plan_id=plan_id,
            status="active",
            current_period_start=start,
            current_period_end=end,
            stripe_subscription_id="sub_9876543210",
            created_at=created_at,
            updated_at=updated_at,
        )

        assert sub.id == sub_id
        assert sub.account_id == account_id
        assert sub.plan_id == plan_id
        assert sub.status == "active"
        assert sub.stripe_subscription_id == "sub_9876543210"
        assert sub.created_at == created_at
        assert sub.updated_at == updated_at

    def test_from_attributes_config(self):
        """Test that from_attributes is enabled in Config."""
        assert Subscription.model_config.get("from_attributes") is True


class TestMonthlyUsageBase:
    """Test MonthlyUsageBase schema."""

    def test_create_with_required_fields(self):
        """Test creating MonthlyUsageBase with required fields."""
        subscription_id = uuid4()
        start_date = date(2025, 1, 1)
        end_date = date(2025, 1, 31)

        usage = MonthlyUsageBase(
            subscription_id=subscription_id,
            billing_cycle_start=start_date,
            billing_cycle_end=end_date,
        )

        assert usage.subscription_id == subscription_id
        assert usage.billing_cycle_start == start_date
        assert usage.billing_cycle_end == end_date
        assert usage.usage_counts == {}  # default value

    def test_create_with_usage_counts(self):
        """Test creating MonthlyUsageBase with usage counts."""
        subscription_id = uuid4()
        start_date = date(2025, 1, 1)
        end_date = date(2025, 1, 31)

        usage = MonthlyUsageBase(
            subscription_id=subscription_id,
            billing_cycle_start=start_date,
            billing_cycle_end=end_date,
            usage_counts={
                "api_calls": 8500,
                "ai_calls": 750,
                "issues_ingested": 4200,
            },
        )

        assert usage.usage_counts["api_calls"] == 8500
        assert usage.usage_counts["ai_calls"] == 750
        assert usage.usage_counts["issues_ingested"] == 4200


class TestMonthlyUsageCreate:
    """Test MonthlyUsageCreate schema."""

    def test_inherits_from_base(self):
        """Test that MonthlyUsageCreate inherits from MonthlyUsageBase."""
        subscription_id = uuid4()
        start_date = date(2025, 1, 1)
        end_date = date(2025, 1, 31)

        usage = MonthlyUsageCreate(
            subscription_id=subscription_id,
            billing_cycle_start=start_date,
            billing_cycle_end=end_date,
        )

        assert isinstance(usage, MonthlyUsageBase)
        assert usage.subscription_id == subscription_id


class TestMonthlyUsage:
    """Test MonthlyUsage schema."""

    def test_create_complete_monthly_usage(self):
        """Test creating complete MonthlyUsage with all fields."""
        usage_id = uuid4()
        subscription_id = uuid4()
        start_date = date(2025, 1, 1)
        end_date = date(2025, 1, 31)
        created_at = datetime.now()
        updated_at = datetime.now()

        usage = MonthlyUsage(
            id=usage_id,
            subscription_id=subscription_id,
            billing_cycle_start=start_date,
            billing_cycle_end=end_date,
            usage_counts={
                "api_calls": 9500,
                "ai_calls": 950,
                "issues_ingested": 4800,
            },
            created_at=created_at,
            updated_at=updated_at,
        )

        assert usage.id == usage_id
        assert usage.subscription_id == subscription_id
        assert usage.billing_cycle_start == start_date
        assert usage.billing_cycle_end == end_date
        assert usage.usage_counts["api_calls"] == 9500
        assert usage.created_at == created_at
        assert usage.updated_at == updated_at

    def test_from_attributes_config(self):
        """Test that from_attributes is enabled in Config."""
        assert MonthlyUsage.model_config.get("from_attributes") is True

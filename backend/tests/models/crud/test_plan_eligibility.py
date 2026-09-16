"""Plan eligibility: the numbers the console shows instead of adjectives.

Every limit used here comes from a fixture, never from ``plans.yaml``: the
catalog's numbers are a business decision that changes without notice, and a
test that hard-codes "Pro includes 1 user" fails the day pricing moves while
proving nothing about the computation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from preloop.models import models
from preloop.models.crud import crud_account, crud_user
from preloop.models.crud import plan_eligibility
from preloop.models.crud.plan_eligibility import (
    AccountState,
    PlanLimits,
    account_state,
    analytics_data_age,
    evaluate_plan,
    evaluate_plans,
    ingest_tokens_this_month,
    protected_floor_days,
    retention_effect,
)

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def _limits(plan_id: str = "target", **overrides) -> PlanLimits:
    values = {
        "plan_id": plan_id,
        "name": plan_id.title(),
        "included_users": 1,
        "max_users": 1,
        "max_agents": -1,
        "ingest_tokens_monthly": None,
        "retention_days": -1,
    }
    values.update(overrides)
    return PlanLimits(**values)


def _account(db, name_hint: str = "eligibility"):
    return crud_account.create(
        db,
        obj_in={
            "organization_name": f"{name_hint} {uuid.uuid4().hex[:8]}",
            "is_active": True,
        },
    )


def _user(db, account):
    unique = uuid.uuid4().hex[:8]
    return crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "email": f"eligibility_{unique}@example.com",
            "username": f"eligibility_{unique}",
            "hashed_password": "x",
            "is_active": True,
        },
    )


def _usage(db, account, *, when: datetime, tokens: int | None = 10, **overrides):
    values = {
        "account_id": account.id,
        "endpoint": "/v1/chat/completions",
        "method": "POST",
        "status_code": 200,
        "duration": 0.1,
        "action_type": "model_gateway",
        "total_tokens": tokens,
        "timestamp": when,
    }
    values.update(overrides)
    row = models.ApiUsage(**values)
    db.add(row)
    db.flush()
    return row


def _session(db, account, *, started_at: datetime, legal_hold: bool = False):
    row = models.RuntimeSession(
        account_id=account.id,
        session_source_type="test",
        session_source_id=uuid.uuid4().hex,
        started_at=started_at,
        last_activity_at=started_at,
        legal_hold=legal_hold,
    )
    db.add(row)
    db.flush()
    return row


class TestSeatsAndAgents:
    """The two limits the platform actually refuses on, so the switch does too."""

    def test_members_over_the_cap_block_with_the_arithmetic(self):
        result = evaluate_plan(
            _limits("pro", name="Pro", included_users=1, max_users=1),
            AccountState(active_users=4),
            now=NOW,
        )
        assert result["eligible"] is False
        blocker = result["blockers"][0]
        assert blocker["kind"] == "members"
        assert blocker["current"] == 4 and blocker["limit"] == 1
        assert blocker["message"] == (
            "You have 4 members; Pro includes 1. Remove 3 members to switch."
        )

    def test_pending_invitations_count_and_are_named(self):
        result = evaluate_plan(
            _limits("pro", name="Pro"),
            AccountState(active_users=3, pending_invitations=1),
            now=NOW,
        )
        assert result["blockers"][0]["current"] == 4
        assert result["blockers"][0]["message"] == (
            "You have 4 members (3 active users and 1 pending invitation); "
            "Pro includes 1. Remove 3 members to switch."
        )

    def test_seat_addon_ceiling_is_the_refusal_point_and_is_explained(self):
        limits = _limits("business", name="Business", included_users=20, max_users=50)
        assert evaluate_plan(limits, AccountState(active_users=44), now=NOW)["eligible"]
        blocked = evaluate_plan(limits, AccountState(active_users=51), now=NOW)
        assert blocked["eligible"] is False
        assert blocked["blockers"][0]["message"] == (
            "You have 51 members; Business includes 20 and allows up to 50 "
            "with the seat add-on. Remove 1 member to switch."
        )

    def test_unlimited_seats_never_block(self):
        result = evaluate_plan(
            _limits(included_users=-1, max_users=-1),
            AccountState(active_users=900, pending_invitations=40),
            now=NOW,
        )
        assert result["eligible"] is True and result["blockers"] == []

    def test_agents_over_the_cap_block_and_promise_no_deletion(self):
        result = evaluate_plan(
            _limits("free", name="Free", max_users=-1, included_users=-1, max_agents=3),
            AccountState(active_agents=12),
            now=NOW,
        )
        assert result["eligible"] is False
        blocker = result["blockers"][0]
        assert blocker["kind"] == "agents"
        assert blocker["current"] == 12 and blocker["limit"] == 3
        assert blocker["message"] == (
            "You have 12 active agents; Free includes 3. Deactivate 9 agents "
            "to switch. A plan change never deletes an agent."
        )

    def test_at_the_limit_is_eligible_not_blocked(self):
        result = evaluate_plan(
            _limits(max_users=5, included_users=5, max_agents=3),
            AccountState(active_users=4, pending_invitations=1, active_agents=3),
            now=NOW,
        )
        assert result["eligible"] is True and result["blockers"] == []

    def test_a_fitting_plan_says_nothing_at_all(self):
        result = evaluate_plan(
            _limits(max_users=5, included_users=5, ingest_tokens_monthly=1_000),
            AccountState(active_users=2, ingest_tokens_this_month=10),
            now=NOW,
        )
        assert result == {
            "plan_id": "target",
            "eligible": True,
            "blockers": [],
            "warnings": [],
            "retention": result["retention"],
        }
        assert result["retention"]["message"] is None
        assert result["retention"]["benefit_message"] is None


class TestIngestionQuota:
    """Over quota degrades analytics detail; it never refuses a switch."""

    def test_over_quota_warns_with_both_numbers_and_stays_eligible(self):
        result = evaluate_plan(
            _limits("pro", name="Pro", max_users=-1, ingest_tokens_monthly=100_000),
            AccountState(ingest_tokens_this_month=142_000),
            now=NOW,
        )
        assert result["eligible"] is True
        warning = result["warnings"][0]
        assert warning["kind"] == "ingest"
        assert warning["current"] == 142_000 and warning["limit"] == 100_000
        assert warning["message"] == (
            "You have ingested 142,000 tokens so far this month; Pro includes "
            "100,000 per month. Above the quota analytics detail is reduced; "
            "the gateway, firewall, approvals and budgets keep running."
        )

    def test_within_quota_unlimited_quota_and_unmeasured_usage_say_nothing(self):
        within = AccountState(ingest_tokens_this_month=10)
        assert (
            evaluate_plan(
                _limits(max_users=-1, ingest_tokens_monthly=100), within, now=NOW
            )["warnings"]
            == []
        )
        assert (
            evaluate_plan(
                _limits(max_users=-1, ingest_tokens_monthly=-1),
                AccountState(ingest_tokens_this_month=10**12),
                now=NOW,
            )["warnings"]
            == []
        )
        assert (
            evaluate_plan(
                _limits(max_users=-1, ingest_tokens_monthly=100),
                AccountState(ingest_tokens_this_month=None),
                now=NOW,
            )["warnings"]
            == []
        )


class TestRetention:
    """Warn when records would be deleted, and only then."""

    def test_shorter_window_over_older_data_names_the_cutoff(self):
        state = AccountState(
            oldest_record_at=datetime(2025, 2, 11, tzinfo=timezone.utc),
            oldest_record_class="usage",
            current_retention_days=730,
        )
        retention = retention_effect(
            _limits("team", name="Team", retention_days=365), state, now=NOW
        )
        assert retention["affected"] is True
        assert retention["target_days"] == 365
        assert retention["cutoff_at"].startswith("2025-09-16")
        assert retention["message"] == (
            "Your usage records go back to 2025-02-11. Team keeps 365 days, "
            "so records before 2025-09-16 will be deleted after the switch."
        )

    def test_a_shorter_window_with_no_old_data_is_silent(self):
        state = AccountState(
            oldest_record_at=NOW - timedelta(days=40),
            oldest_record_class="usage",
            current_retention_days=730,
        )
        retention = retention_effect(
            _limits("free", name="Free", retention_days=183), state, now=NOW
        )
        assert retention["affected"] is False and retention["message"] is None

    def test_an_account_with_no_analytics_is_silent(self):
        retention = retention_effect(
            _limits(retention_days=183),
            AccountState(current_retention_days=730),
            now=NOW,
        )
        assert retention["affected"] is False and retention["message"] is None

    def test_a_longer_window_is_a_benefit_line_not_a_warning(self):
        state = AccountState(
            oldest_record_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            oldest_record_class="usage",
            current_retention_days=183,
        )
        retention = retention_effect(
            _limits("team", name="Team", retention_days=730), state, now=NOW
        )
        assert retention["affected"] is False and retention["message"] is None
        assert retention["benefit_message"] == (
            "Analytics history extends from 183 to 730 days. Records already "
            "deleted are not restored."
        )

    def test_an_unlimited_window_is_a_benefit_line(self):
        retention = retention_effect(
            _limits("enterprise", name="Enterprise", retention_days=-1),
            AccountState(current_retention_days=365),
            now=NOW,
        )
        assert retention["target_days"] is None and retention["affected"] is False
        assert retention["benefit_message"] == (
            "Enterprise keeps analytics history without a time limit, up from 365 days."
        )

    def test_the_same_window_says_nothing_either_way(self):
        state = AccountState(
            oldest_record_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            oldest_record_class="usage",
            current_retention_days=365,
        )
        retention = retention_effect(_limits(retention_days=365), state, now=NOW)
        assert retention["affected"] is False
        assert retention["message"] is None and retention["benefit_message"] is None

    def test_a_grandfathered_floor_protects_instead_of_warning(self):
        state = AccountState(
            oldest_record_at=datetime(2026, 2, 11, tzinfo=timezone.utc),
            oldest_record_class="runtime_sessions",
            current_retention_days=-1,
            retention_floor_days=730,
        )
        retention = retention_effect(
            _limits("free", name="Free", retention_days=183), state, now=NOW
        )
        assert retention["affected"] is False
        assert retention["protected_by_floor"] is True
        assert retention["message"] == (
            "Your runtime sessions go back to 2026-02-11. Free keeps 183 "
            "days, but your account already keeps analytics for 730 days, so "
            "this switch deletes nothing."
        )

    def test_a_legal_hold_is_named_in_both_outcomes(self):
        state = AccountState(
            oldest_record_at=datetime(2026, 2, 11, tzinfo=timezone.utc),
            oldest_record_class="runtime_sessions",
            current_retention_days=730,
            legal_hold=True,
        )
        warned = retention_effect(
            _limits("free", name="Free", retention_days=183), state, now=NOW
        )
        assert warned["affected"] is True
        assert warned["message"].endswith(
            "Records under a legal hold are kept for as long as the hold lasts."
        )

    def test_an_unlimited_current_window_still_shrinks_on_a_finite_target(self):
        state = AccountState(
            oldest_record_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            oldest_record_class="usage",
            current_retention_days=-1,
        )
        retention = retention_effect(_limits(retention_days=365), state, now=NOW)
        assert retention["affected"] is True

    def test_an_affected_window_is_also_a_warning_entry(self):
        result = evaluate_plan(
            _limits("free", name="Free", max_users=-1, retention_days=183),
            AccountState(
                oldest_record_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
                oldest_record_class="usage",
                current_retention_days=730,
            ),
            now=NOW,
        )
        assert result["eligible"] is True
        assert [w["kind"] for w in result["warnings"]] == ["retention"]


class TestCandidateList:
    """A quote-only plan is never a checkout target; the caller filters it out."""

    def test_every_candidate_is_answered_in_order(self):
        results = evaluate_plans(
            [
                _limits("free", name="Free", max_users=1, included_users=1),
                _limits("team", name="Team", max_users=5, included_users=5),
            ],
            AccountState(active_users=4),
            now=NOW,
        )
        assert [r["plan_id"] for r in results] == ["free", "team"]
        assert [r["eligible"] for r in results] == [False, True]

    def test_a_quote_only_plan_is_marked_not_purchasable(self):
        assert _limits("enterprise", purchasable=False).purchasable is False
        assert _limits("team").purchasable is True


class TestMeasuredState:
    """The database halves: counted rows, not assumptions."""

    def test_ingestion_counts_this_month_only_and_skips_replay(self, db_session):
        account = _account(db_session)
        _usage(db_session, account, when=NOW - timedelta(days=1), tokens=100)
        _usage(db_session, account, when=NOW - timedelta(days=45), tokens=900)
        _usage(
            db_session,
            account,
            when=NOW - timedelta(days=1),
            tokens=500,
            meta_data={"purpose": "replay_validation"},
        )
        _usage(
            db_session,
            account,
            when=NOW - timedelta(days=1),
            tokens=700,
            action_type="api_call",
        )
        _usage(db_session, account, when=NOW - timedelta(days=1), tokens=None)
        assert (
            ingest_tokens_this_month(db_session, account_id=str(account.id), now=NOW)
            == 100
        )

    def test_ingestion_restricted_to_named_models(self, db_session):
        account = _account(db_session)
        model = models.AIModel(
            account_id=account.id,
            name=f"model-{uuid.uuid4().hex[:8]}",
            provider_name="openai",
            model_identifier="gpt-4o-mini",
        )
        db_session.add(model)
        db_session.flush()
        _usage(
            db_session,
            account,
            when=NOW - timedelta(days=1),
            tokens=100,
            ai_model_id=model.id,
        )
        _usage(db_session, account, when=NOW - timedelta(days=1), tokens=7)
        assert (
            ingest_tokens_this_month(
                db_session,
                account_id=str(account.id),
                now=NOW,
                model_ids=[str(model.id)],
            )
            == 100
        )
        assert (
            ingest_tokens_this_month(
                db_session, account_id=str(account.id), now=NOW, model_ids=[]
            )
            == 0
        )

    def test_oldest_record_spans_both_analytics_classes(self, db_session):
        account = _account(db_session)
        assert analytics_data_age(db_session, account_id=str(account.id)) == {
            "per_class": {"usage": None, "runtime_sessions": None},
            "oldest_record_at": None,
            "oldest_record_class": None,
            "legal_hold": False,
        }
        _usage(db_session, account, when=datetime(2026, 2, 11, tzinfo=timezone.utc))
        _session(
            db_session, account, started_at=datetime(2025, 12, 1, tzinfo=timezone.utc)
        )
        age = analytics_data_age(db_session, account_id=str(account.id))
        assert age["oldest_record_class"] == "runtime_sessions"
        assert age["oldest_record_at"].startswith("2025-12-01")
        assert age["per_class"]["usage"].startswith("2026-02-11")
        assert age["legal_hold"] is False

    def test_a_held_session_is_reported(self, db_session):
        account = _account(db_session)
        _session(
            db_session,
            account,
            started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
            legal_hold=True,
        )
        assert analytics_data_age(db_session, account_id=str(account.id))["legal_hold"]

    def test_the_account_floor_is_the_longest_promise_held(self, db_session):
        account = _account(db_session)
        assert protected_floor_days(db_session, account) is None
        account.subscription_history_retention_days = 730
        db_session.flush()
        assert protected_floor_days(db_session, account) == 730

    def test_account_state_measures_every_input_once(self, db_session):
        account = _account(db_session)
        _user(db_session, account)
        _usage(db_session, account, when=NOW - timedelta(days=2), tokens=42)
        state = account_state(
            db_session,
            account,
            counts={"active_users": 4, "pending_invitations": 1, "active_agents": 2},
            now=NOW,
            current_retention_days=365,
        )
        assert state.members == 5
        assert state.active_agents == 2
        assert state.ingest_tokens_this_month == 42
        assert state.oldest_record_class == "usage"
        assert state.current_retention_days == 365
        assert state.retention_floor_days is None

    def test_month_start_is_utc_midnight_on_the_first(self):
        assert plan_eligibility.month_start(NOW) == datetime(
            2026, 9, 1, tzinfo=timezone.utc
        )

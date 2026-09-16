"""Trimming one account to its plan's caps: order, idempotency and audit.

The policy that decides *whether* an account is trimmed lives in the optional
enterprise billing plugin. These tests cover the persistence primitive it
calls: who survives a smaller plan, who is deactivated, and what is recorded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

import pytest

from preloop.models import models
from preloop.models.crud import crud_account, crud_user
from preloop.models.crud.billing import billing

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)


def _account(db):
    return crud_account.create(
        db,
        obj_in={
            "organization_name": f"plan fit {uuid.uuid4().hex[:8]}",
            "is_active": True,
        },
    )


def _user(db, account, *, last_login=None, owner=False):
    unique = uuid.uuid4().hex[:8]
    user = crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "email": f"fit_{unique}@example.com",
            "username": f"fit_{unique}",
            "hashed_password": "x",
            "is_active": True,
        },
    )
    user.last_login = last_login
    db.add(user)
    if owner:
        account.primary_user_id = user.id
        db.add(account)
    db.flush()
    return user


def _agent(db, account, *, last_seen_at, connected: bool = False):
    agent = models.ManagedAgent(
        account_id=account.id,
        agent_kind="cli",
        session_source_type="test",
        session_source_id=uuid.uuid4().hex,
        display_name=f"agent {uuid.uuid4().hex[:6]}",
        lifecycle_state="active",
        lifecycle_updated_at=NOW,
        last_seen_at=last_seen_at,
        control_connection_id=uuid.uuid4().hex if connected else None,
        control_last_heartbeat_at=last_seen_at if connected else None,
        control_session_mode="interactive" if connected else None,
    )
    db.add(agent)
    db.flush()
    return agent


def _enforce(db, account, *, max_users, max_agents, key=None):
    return billing.enforce_plan_fit(
        db,
        account_id=str(account.id),
        max_users=max_users,
        max_agents=max_agents,
        reason="plan_downgrade",
        operation_key=key or f"plan-fit-{uuid.uuid4().hex[:8]}",
        payload={"plan_id": "free"},
    )


class TestPlanFitCandidates:
    def test_the_owner_is_first_even_without_a_login(self, db_session):
        account = _account(db_session)
        owner = _user(db_session, account, owner=True, last_login=None)
        recent = _user(db_session, account, last_login=NOW)

        order = billing.plan_fit_candidates(db_session, str(account.id))["users"]

        assert [user.id for user in order] == [owner.id, recent.id]

    def test_members_are_ordered_by_last_login_with_never_logged_in_last(
        self, db_session
    ):
        account = _account(db_session)
        _user(db_session, account, owner=True, last_login=NOW)
        stale = _user(db_session, account, last_login=NOW - timedelta(days=30))
        active = _user(db_session, account, last_login=NOW - timedelta(days=1))
        never = _user(db_session, account, last_login=None)

        order = billing.plan_fit_candidates(db_session, str(account.id))["users"]

        assert [user.id for user in order[1:]] == [active.id, stale.id, never.id]

    def test_agents_are_ordered_by_last_activity(self, db_session):
        account = _account(db_session)
        old = _agent(db_session, account, last_seen_at=NOW - timedelta(days=10))
        fresh = _agent(db_session, account, last_seen_at=NOW)

        order = billing.plan_fit_candidates(db_session, str(account.id))["agents"]

        assert [agent.id for agent in order] == [fresh.id, old.id]

    def test_inactive_rows_are_not_candidates(self, db_session):
        account = _account(db_session)
        owner = _user(db_session, account, owner=True, last_login=NOW)
        gone = _user(db_session, account, last_login=NOW)
        gone.is_active = False
        retired = _agent(db_session, account, last_seen_at=NOW)
        retired.lifecycle_state = "retired"
        db_session.flush()

        candidates = billing.plan_fit_candidates(db_session, str(account.id))

        assert [user.id for user in candidates["users"]] == [owner.id]
        assert candidates["agents"] == []


class TestEnforcePlanFit:
    def test_surplus_members_are_deactivated_and_the_owner_is_kept(self, db_session):
        account = _account(db_session)
        owner = _user(db_session, account, owner=True, last_login=None)
        keep = _user(db_session, account, last_login=NOW)
        drop = _user(db_session, account, last_login=NOW - timedelta(days=9))

        result = _enforce(db_session, account, max_users=2, max_agents=-1)

        assert result["status"] == "applied"
        assert [row["id"] for row in result["users"]] == [str(drop.id)]
        db_session.refresh(owner)
        db_session.refresh(keep)
        db_session.refresh(drop)
        assert owner.is_active is True
        assert keep.is_active is True
        assert drop.is_active is False

    def test_surplus_agents_are_suspended_and_disconnected(self, db_session):
        account = _account(db_session)
        _user(db_session, account, owner=True, last_login=NOW)
        keep = _agent(db_session, account, last_seen_at=NOW, connected=True)
        drop = _agent(
            db_session, account, last_seen_at=NOW - timedelta(days=2), connected=True
        )

        result = _enforce(db_session, account, max_users=-1, max_agents=1)

        assert [row["id"] for row in result["agents"]] == [str(drop.id)]
        db_session.refresh(keep)
        db_session.refresh(drop)
        assert keep.lifecycle_state == "active"
        assert keep.control_connection_id is not None
        assert drop.lifecycle_state == "suspended"
        assert drop.lifecycle_reason == "plan_downgrade"
        assert drop.control_connection_id is None
        assert drop.control_last_heartbeat_at is None
        assert drop.control_session_mode is None

    def test_an_account_inside_its_caps_is_left_alone(self, db_session):
        account = _account(db_session)
        _user(db_session, account, owner=True, last_login=NOW)
        _agent(db_session, account, last_seen_at=NOW)

        result = _enforce(db_session, account, max_users=1, max_agents=3)

        assert result == {
            "status": "within_limits",
            "users": [],
            "agents": [],
            "operation_id": None,
        }
        assert (
            db_session.query(models.BillingOperation)
            .filter(models.BillingOperation.account_id == account.id)
            .count()
            == 0
        )

    @pytest.mark.parametrize("caps", [(-1, -1), (-1, 0), (0, -1)])
    def test_an_unlimited_cap_trims_nothing_on_its_side(self, db_session, caps):
        max_users, max_agents = caps
        account = _account(db_session)
        _user(db_session, account, owner=True, last_login=NOW)
        _user(db_session, account, last_login=NOW)
        _agent(db_session, account, last_seen_at=NOW)

        result = _enforce(
            db_session, account, max_users=max_users, max_agents=max_agents
        )

        if max_users == -1:
            assert result["users"] == []
        if max_agents == -1:
            assert result["agents"] == []

    def test_the_change_is_audited(self, db_session):
        account = _account(db_session)
        _user(db_session, account, owner=True, last_login=NOW)
        _user(db_session, account, last_login=NOW)

        result = _enforce(db_session, account, max_users=1, max_agents=-1)

        row = db_session.get(models.BillingOperation, uuid.UUID(result["operation_id"]))
        assert row.kind == "plan_fit_enforcement"
        assert row.status == "completed"
        assert row.payload == {"plan_id": "free"}
        assert row.result["reason"] == "plan_downgrade"
        assert row.lease_until is None

    def test_replaying_one_key_changes_nothing_more(self, db_session):
        account = _account(db_session)
        _user(db_session, account, owner=True, last_login=NOW)
        _user(db_session, account, last_login=NOW)
        survivor = _user(db_session, account, last_login=NOW)
        key = f"plan-fit-{uuid.uuid4().hex[:8]}"

        first = _enforce(db_session, account, max_users=2, max_agents=-1, key=key)
        # A later reactivation must not be undone by a replayed enforcement.
        deactivated = db_session.get(models.User, uuid.UUID(first["users"][0]["id"]))
        deactivated.is_active = True
        db_session.flush()
        second = _enforce(db_session, account, max_users=2, max_agents=-1, key=key)

        assert first["status"] == "applied"
        assert second["status"] == "already_applied"
        assert second["users"] == first["users"]
        db_session.refresh(deactivated)
        db_session.refresh(survivor)
        assert deactivated.is_active is True
        assert survivor.is_active is True
        assert (
            db_session.query(models.BillingOperation)
            .filter(models.BillingOperation.account_id == account.id)
            .count()
            == 1
        )

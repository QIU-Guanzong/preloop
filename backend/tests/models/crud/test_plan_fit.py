"""Trimming one account to its plan's caps: order, idempotency and audit.

The policy that decides *whether* an account is trimmed lives in the optional
enterprise billing plugin. These tests cover the persistence primitive it
calls: who survives a smaller plan, who is deactivated, and what is recorded.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import threading
import uuid

import pytest
from sqlalchemy.orm import Session

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

    def test_the_owner_survives_a_cap_that_leaves_no_seats(self, db_session):
        """No catalog plan sets 0 seats, and if one ever did, the owner stays.

        Deactivating the owner locks every human out of the account, including
        out of the upgrade that would undo the trim.
        """
        account = _account(db_session)
        owner = _user(db_session, account, owner=True, last_login=NOW)
        member = _user(db_session, account, last_login=NOW)

        result = _enforce(db_session, account, max_users=0, max_agents=-1)

        assert [row["id"] for row in result["users"]] == [str(member.id)]
        db_session.refresh(owner)
        db_session.refresh(member)
        assert owner.is_active is True
        assert member.is_active is False

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
        owner = _user(db_session, account, owner=True, last_login=NOW)
        _user(db_session, account, last_login=NOW)
        _agent(db_session, account, last_seen_at=NOW)

        result = _enforce(
            db_session, account, max_users=max_users, max_agents=max_agents
        )

        if max_users == -1:
            assert result["users"] == []
        if max_agents == -1:
            assert result["agents"] == []
        # Every case, including the zero seat cap: the owner always keeps a
        # seat, so no cap can ever lock the account's administrator out.
        db_session.refresh(owner)
        assert owner.is_active is True
        assert str(owner.id) not in {row["id"] for row in result["users"]}

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


class TestEnforcePlanFitRace:
    """Two enforcer triggers landing on one account with the same key.

    The webhook path, the scheduled reconcile and an immediate plan switch can
    all fire for the same downgrade, so the same ``operation_key`` really does
    arrive twice at once. These run on independent connections against
    committed rows, because the race is a database lock question and the
    transactional test session cannot express it.
    """

    @staticmethod
    def _committed_account(engine):
        """Account with an owner and two surplus members, visible to any connection."""
        account_id, owner_id = uuid.uuid4(), uuid.uuid4()
        member_ids = [uuid.uuid4(), uuid.uuid4()]
        with Session(engine) as db:
            db.add(
                models.Account(
                    id=account_id,
                    organization_name=f"plan fit race {account_id.hex[:8]}",
                    is_active=True,
                )
            )
            db.flush()
            for index, user_id in enumerate([owner_id, *member_ids]):
                db.add(
                    models.User(
                        id=user_id,
                        account_id=account_id,
                        username=f"race_{user_id.hex[:10]}",
                        email=f"race_{user_id.hex[:10]}@example.com",
                        hashed_password="x",
                        is_active=True,
                        last_login=NOW - timedelta(minutes=index),
                    )
                )
            db.flush()
            db.query(models.Account).filter(models.Account.id == account_id).update(
                {"primary_user_id": owner_id}
            )
            db.commit()
        return account_id, owner_id, member_ids

    def test_two_sessions_with_one_key_apply_once_and_replay_once(self, db_engine):
        """The loser blocks on the account lock and reads the winner's row.

        Before the lock came first, both callers passed the idempotency check
        on their own snapshot and only serialized on the write, so the loser
        reported ``within_limits`` (or collided on uq_billing_operation_key)
        instead of the documented ``already_applied`` replay.
        """
        account_id, owner_id, member_ids = self._committed_account(db_engine)
        key = f"plan-fit-race-{uuid.uuid4().hex[:8]}"
        start = threading.Barrier(2)
        results: list[dict] = []
        failures: list[BaseException] = []

        def enforce() -> None:
            try:
                with Session(db_engine) as db:
                    start.wait(timeout=10)
                    results.append(
                        billing.enforce_plan_fit(
                            db,
                            account_id=str(account_id),
                            max_users=1,
                            max_agents=-1,
                            reason="plan_downgrade",
                            operation_key=key,
                            payload={"plan_id": "free"},
                        )
                    )
            except BaseException as exc:  # noqa: BLE001 - reported to the test
                failures.append(exc)

        threads = [threading.Thread(target=enforce) for _ in range(2)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            assert not failures, failures
            assert not any(thread.is_alive() for thread in threads)
            assert sorted(result["status"] for result in results) == [
                "already_applied",
                "applied",
            ]
            # Both callers report the same outcome, so the notification the
            # enforcer sends is the same whichever one it came from.
            assert results[0]["users"] == results[1]["users"]

            with Session(db_engine) as db:
                rows = (
                    db.query(models.BillingOperation)
                    .filter(models.BillingOperation.account_id == account_id)
                    .all()
                )
                assert len(rows) == 1
                assert rows[0].operation_key == key
                assert rows[0].status == "completed"
                states = {
                    str(user.id): user.is_active
                    for user in db.query(models.User)
                    .filter(models.User.account_id == account_id)
                    .all()
                }
            assert states[str(owner_id)] is True
            assert [states[str(member_id)] for member_id in member_ids] == [
                False,
                False,
            ]
        finally:
            with Session(db_engine) as db:
                db.query(models.BillingOperation).filter(
                    models.BillingOperation.account_id == account_id
                ).delete()
                db.query(models.User).filter(
                    models.User.account_id == account_id
                ).delete()
                db.query(models.Account).filter(
                    models.Account.id == account_id
                ).delete()
                db.commit()

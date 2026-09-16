"""One-shot CI runners must disappear, not linger as offline rows."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
from preloop.models.crud.flow_runner import ONLINE_HEARTBEAT_TTL, crud_flow_runner
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate


def _account(db: Session, name: str) -> models.Account:
    return crud_account.create(db, obj_in={"organization_name": name})


def _runner(db: Session, account_id, **overrides) -> models.FlowRunner:
    obj_in = {
        "account_id": account_id,
        "name": f"runner-{uuid4()}",
        "token_hash": f"hash-{uuid4()}",
        "status": "online",
        "last_heartbeat": datetime.now(timezone.utc),
    }
    obj_in.update(overrides)
    return crud_flow_runner.create(db, obj_in=obj_in)


def _execution(db: Session, account_id) -> models.FlowExecution:
    """A real row: flow_runner.current_execution_id carries a foreign key."""
    flow = crud_flow.create(
        db=db,
        flow_in=FlowCreate(
            name=f"ephemeral-sweep-{uuid4()}",
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=account_id,
        ),
        account_id=account_id,
    )
    return crud_flow_execution.create(
        db, FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
    )


def test_runners_are_persistent_unless_registered_ephemeral(
    db_session: Session,
) -> None:
    account = _account(db_session, "Ephemeral default")
    runner = _runner(db_session, account.id)
    assert runner.ephemeral is False


def test_mark_ephemeral_is_idempotent(db_session: Session) -> None:
    """A reconnecting one-shot runner re-asserts the flag on every heartbeat."""
    account = _account(db_session, "Ephemeral mark")
    runner = _runner(db_session, account.id)

    assert crud_flow_runner.mark_ephemeral(db_session, runner_id=runner.id) is True
    db_session.refresh(runner)
    assert runner.ephemeral is True
    # The second call changes no row, so callers can skip the refresh.
    assert crud_flow_runner.mark_ephemeral(db_session, runner_id=runner.id) is False


def test_sweep_deletes_only_lapsed_idle_ephemeral_runners(
    db_session: Session,
) -> None:
    account = _account(db_session, "Ephemeral sweep")
    stale_at = datetime.now(timezone.utc) - ONLINE_HEARTBEAT_TTL - timedelta(seconds=5)

    doomed = _runner(db_session, account.id, ephemeral=True, last_heartbeat=stale_at)
    alive = _runner(db_session, account.id, ephemeral=True)
    busy = _runner(
        db_session,
        account.id,
        ephemeral=True,
        last_heartbeat=stale_at,
        status="busy",
    )
    # A persistent runner going quiet is an outage to show, not a row to drop.
    persistent = _runner(db_session, account.id, last_heartbeat=stale_at)

    busy.current_execution_id = _execution(db_session, account.id).id
    db_session.add(busy)
    db_session.commit()

    doomed_id, alive_id = doomed.id, alive.id
    busy_id, persistent_id = busy.id, persistent.id

    assert (
        crud_flow_runner.sweep_stale_ephemeral(db_session, account_id=account.id) == 1
    )

    remaining = {
        row.id
        for row in crud_flow_runner.list_for_account(db_session, account_id=account.id)
    }
    assert doomed_id not in remaining
    assert {alive_id, busy_id, persistent_id} <= remaining


def test_sweep_is_scoped_to_one_account(db_session: Session) -> None:
    """A CI account reaping its own rows must not touch a neighbour's."""
    mine = _account(db_session, "Ephemeral mine")
    theirs = _account(db_session, "Ephemeral theirs")
    stale_at = datetime.now(timezone.utc) - ONLINE_HEARTBEAT_TTL - timedelta(seconds=5)
    _runner(db_session, mine.id, ephemeral=True, last_heartbeat=stale_at)
    neighbour = _runner(db_session, theirs.id, ephemeral=True, last_heartbeat=stale_at)

    assert crud_flow_runner.sweep_stale_ephemeral(db_session, account_id=mine.id) == 1
    assert crud_flow_runner.get_fresh(db_session, runner_id=neighbour.id) is not None


def test_sweep_spares_a_runner_that_has_not_connected_yet(
    db_session: Session,
) -> None:
    """Registration precedes the first heartbeat; the gap is not a death."""
    account = _account(db_session, "Ephemeral warmup")
    fresh = _runner(db_session, account.id, ephemeral=True, last_heartbeat=None)

    assert (
        crud_flow_runner.sweep_stale_ephemeral(db_session, account_id=account.id) == 0
    )
    assert crud_flow_runner.get_fresh(db_session, runner_id=fresh.id) is not None

    # With no grace at all (the unregister path), the same row does go.
    assert (
        crud_flow_runner.sweep_stale_ephemeral(
            db_session, account_id=account.id, grace=timedelta(0)
        )
        == 1
    )

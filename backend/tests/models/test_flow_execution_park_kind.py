"""The ``park_kind`` column: what a parked run is waiting on (#633).

A park on a human and a park on children are the same three durable steps on
the same columns, so the row has to say which one it is while the status is
still RUNNING. These tests pin the column, the backfill (every park that
exists today is a human one) and the reverse.
"""

import importlib.util
import uuid
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

from preloop.models import models
from preloop.models.crud import crud_flow_execution

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20260915_child_park.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location(
        "child_park_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _downgrade(db_session) -> None:
    with Operations.context(MigrationContext.configure(db_session.connection())):
        _migration().downgrade()


def _upgrade(db_session) -> None:
    with Operations.context(MigrationContext.configure(db_session.connection())):
        _migration().upgrade()


def _execution_columns(db_session) -> set[str]:
    return {
        column["name"]
        for column in inspect(db_session.connection()).get_columns("flow_execution")
    }


def _indexes(db_session) -> set[str]:
    return {
        index["name"]
        for index in inspect(db_session.connection()).get_indexes("flow_execution")
    }


def _legacy_parked_row(db_session, flow_id) -> uuid.UUID:
    """A row parked before the column existed: no kind, but a request id."""
    execution_id = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO flow_execution "
            "(id, flow_id, status, start_time, park_request_id, created_at, "
            " updated_at) "
            "VALUES (:id, :flow_id, 'WAITING_FOR_HUMAN', now(), :request_id, "
            " now(), now())"
        ),
        {"id": execution_id, "flow_id": flow_id, "request_id": uuid.uuid4()},
    )
    return execution_id


def test_the_migration_backfills_existing_parks_as_human(db_session, test_user):
    """An instance that upgrades has only human parks, and says so."""
    from preloop.models.crud import crud_flow
    from preloop.models.schemas.flow import FlowCreate

    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=f"park-kind-{uuid.uuid4().hex[:8]}",
            prompt_template="t",
            agent_type="codex",
            agent_config={},
        ),
        account_id=test_user.account_id,
    )
    db_session.flush()

    _downgrade(db_session)
    assert "park_kind" not in _execution_columns(db_session)
    parked_id = _legacy_parked_row(db_session, flow.id)

    _upgrade(db_session)

    assert "park_kind" in _execution_columns(db_session)
    assert "park_kind" in {
        column.name for column in models.FlowExecution.__table__.columns
    }
    kind = db_session.execute(
        text("SELECT park_kind FROM flow_execution WHERE id = :id"),
        {"id": parked_id},
    ).scalar_one()
    assert kind == "human"


def test_the_migration_reverses_cleanly(db_session):
    """Downgrade drops exactly the column upgrade added."""
    _downgrade(db_session)
    assert "park_kind" not in _execution_columns(db_session)
    assert "ix_flow_execution_child_park_expires_at" not in _indexes(db_session)

    _upgrade(db_session)

    column = {
        entry["name"]: entry
        for entry in inspect(db_session.connection()).get_columns("flow_execution")
    }["park_kind"]
    assert column["nullable"] is True
    assert "ix_flow_execution_child_park_expires_at" not in _indexes(db_session)


def test_a_row_with_no_kind_is_read_as_a_human_park(db_session, test_user):
    """The default is the behaviour that existed before the column."""
    from preloop.models.crud import crud_flow
    from preloop.models.schemas.flow import FlowCreate

    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=f"park-kind-read-{uuid.uuid4().hex[:8]}",
            prompt_template="t",
            agent_type="codex",
            agent_config={},
        ),
        account_id=test_user.account_id,
    )
    db_session.flush()
    parked_id = _legacy_parked_row(db_session, flow.id)

    request = crud_flow_execution.get_park_request(db_session, execution_id=parked_id)

    assert request["kind"] == "human"
    assert crud_flow_execution.parked_status_for_kind(request["kind"]) == (
        "WAITING_FOR_HUMAN"
    )

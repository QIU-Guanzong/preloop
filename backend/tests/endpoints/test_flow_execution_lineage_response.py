"""Lineage survives cold execution reads without serialization queries."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from preloop.api.endpoints import flows
from preloop.models import models, schemas
from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.schemas.flow_execution import (
    FlowExecutionListResponse as LegacyFlowExecutionListResponse,
)

from tests.conftest import maybe_await

LINEAGE_FIELDS = {"parent_execution_id", "root_execution_id", "delegation_depth"}


def _create_lineage(
    db: Session, user: models.User
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a root and child; return scalars safe across identity-map clearing."""
    flow = crud_flow.create(
        db,
        flow_in=FlowCreate(
            name=f"Lineage response {uuid.uuid4()}",
            prompt_template="Test",
            trigger_event_source="manual",
            trigger_event_types=["test"],
            agent_type="codex",
            agent_config={},
            account_id=user.account_id,
        ),
        account_id=user.account_id,
    )
    root = crud_flow_execution.create(
        db, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    child = crud_flow_execution.create(
        db,
        FlowExecutionCreate(
            flow_id=flow.id,
            status="SUCCEEDED",
            parent_execution_id=root.id,
            root_execution_id=root.id,
            delegation_depth=1,
        ),
    )
    return flow.id, root.id, child.id


@pytest.mark.asyncio
async def test_cold_execution_list_projects_lineage_without_lazy_sql(
    db_session: Session, test_user: models.User
) -> None:
    """The real lightweight endpoint returns lineage loaded in its initial read."""
    user_id = test_user.id
    flow_id, root_id, child_id = _create_lineage(db_session, test_user)
    parked_at = datetime.now(timezone.utc)
    expires_at = parked_at + timedelta(hours=1)
    child = crud_flow_execution.get(
        db_session, id=child_id, account_id=test_user.account_id
    )
    assert child is not None
    child.status = "WAITING_FOR_HUMAN"
    child.parked_at = parked_at
    child.park_expires_at = expires_at
    db_session.flush()
    db_session.expunge_all()
    user = db_session.get(models.User, user_id)
    assert user is not None
    result = await maybe_await(
        flows.read_flow_executions(db=db_session, flow_id=flow_id, current_user=user)
    )
    for row in result:
        assert not LINEAGE_FIELDS.intersection(inspect(row).unloaded)

    statements: list[str] = []

    def capture_sql(
        connection: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    connection = db_session.connection()
    event.listen(connection, "before_cursor_execute", capture_sql)
    try:
        for schema in (
            schemas.FlowExecutionListResponse,
            LegacyFlowExecutionListResponse,
        ):
            rows = {
                row.id: schema.model_validate(row).model_dump(mode="json")
                for row in result
            }
            assert rows[child_id]["parent_execution_id"] == str(root_id)
            assert rows[child_id]["root_execution_id"] == str(root_id)
            assert rows[child_id]["delegation_depth"] == 1
            assert rows[root_id]["parent_execution_id"] is None
            assert rows[root_id]["root_execution_id"] is None
            assert rows[root_id]["delegation_depth"] == 0
            if schema is schemas.FlowExecutionListResponse:
                assert datetime.fromisoformat(rows[child_id]["parked_at"]) == parked_at
                assert (
                    datetime.fromisoformat(rows[child_id]["park_expires_at"])
                    == expires_at
                )
    finally:
        event.remove(connection, "before_cursor_execute", capture_sql)
    assert statements == []


@pytest.mark.asyncio
@pytest.mark.parametrize("child", [False, True])
async def test_cold_execution_detail_preserves_lineage_defaults_and_values(
    db_session: Session, test_user: models.User, child: bool
) -> None:
    """Existing detail consumers still receive integer depth and nullable root IDs."""
    user_id = test_user.id
    _, root_id, child_id = _create_lineage(db_session, test_user)
    db_session.expunge_all()
    user = db_session.get(models.User, user_id)
    assert user is not None
    result = await maybe_await(
        flows.read_flow_execution(
            db=db_session,
            execution_id=child_id if child else root_id,
            current_user=user,
        )
    )
    response = schemas.FlowExecutionResponse.model_validate(result).model_dump(
        mode="json"
    )
    assert response["parent_execution_id"] == (str(root_id) if child else None)
    assert response["root_execution_id"] == (str(root_id) if child else None)
    assert response["delegation_depth"] == (1 if child else 0)

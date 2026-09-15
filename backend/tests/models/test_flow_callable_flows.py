"""The ``callable_flows`` delegation allowlist: column, reader helper, clone.

Child of the flows delegation work (#621), issue #627. The column lands with no
enforcement, so these tests pin the shape and the two invariants a later caller
will rely on: an existing flow reads back as "may call nothing", and NULL and
``[]`` are the same thing.
"""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select, text

from preloop.models import models
from preloop.models.crud import crud_flow
from preloop.models.schemas.flow import (
    CallableFlowEntry,
    FlowCreate,
    callable_flows_for,
    parse_callable_flows,
)
from preloop.services.flow_presets_service import clone_preset_for_account

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20260915_flow_callable_flows.py"
)


def _migration():
    """Load the revision off disk, as the sibling migration tests do."""
    spec = importlib.util.spec_from_file_location(
        "flow_callable_flows_migration", MIGRATION_PATH
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


def _flow_columns(db_session) -> set[str]:
    return {
        column["name"]
        for column in inspect(db_session.connection()).get_columns("flow")
    }


@pytest.fixture
def ai_model(db_session, test_user) -> models.AIModel:
    """An account-owned model, so a clone has something to bind to."""
    model = models.AIModel(
        name=f"callable-flows-model-{uuid.uuid4().hex[:8]}",
        provider_name="openai",
        model_identifier="gpt-4o",
        account_id=test_user.account_id,
    )
    db_session.add(model)
    db_session.flush()
    return model


# --- migration shape -------------------------------------------------------


def test_migration_applies_and_existing_flows_read_back_null(db_session, test_user):
    """A flow row written before the upgrade reads back as "no delegation".

    The allowlist has to fail closed on an instance that upgrades: every flow
    it already has must come back NULL, which the reader helper turns into an
    empty list rather than a permission.
    """
    _downgrade(db_session)
    assert "callable_flows" not in _flow_columns(db_session)

    legacy_id = uuid.uuid4()
    db_session.execute(
        text(
            "INSERT INTO flow "
            "(id, name, prompt_template, agent_type, agent_config, "
            " allowed_mcp_servers, allowed_mcp_tools, is_preset, is_enabled, "
            " account_id) "
            "VALUES (:id, :name, 't', 'codex', CAST('{}' AS json), "
            " CAST('[]' AS json), CAST('[]' AS json), false, true, :account_id)"
        ),
        {
            "id": legacy_id,
            "name": f"legacy-{legacy_id.hex[:8]}",
            "account_id": test_user.account_id,
        },
    )

    _upgrade(db_session)

    assert "callable_flows" in _flow_columns(db_session)
    # The model and the revision have to agree, or the next autogenerate run
    # would try to drop what this one added.
    assert "callable_flows" in {column.name for column in models.Flow.__table__.columns}
    legacy = db_session.execute(
        select(models.Flow).filter(models.Flow.id == legacy_id)
    ).scalar_one()
    assert legacy.callable_flows is None
    assert callable_flows_for(legacy) == []


def test_migration_reverses_cleanly(db_session):
    """Downgrade removes exactly the one nullable column upgrade added."""
    _downgrade(db_session)
    assert "callable_flows" not in _flow_columns(db_session)

    _upgrade(db_session)

    column = {
        entry["name"]: entry
        for entry in inspect(db_session.connection()).get_columns("flow")
    }["callable_flows"]
    assert column["nullable"] is True
    # No server default: an absent allowlist is NULL, which is what makes the
    # column fail closed without a backfill.
    assert column["default"] is None


# --- reader helper ---------------------------------------------------------


def test_null_and_empty_list_are_the_same_permission(db_session, test_user):
    """Both mean "this flow may call nothing at all", asserted directly."""
    unset = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=f"unset-{uuid.uuid4().hex[:8]}",
            prompt_template="t",
            agent_type="codex",
            agent_config={},
        ),
        account_id=test_user.account_id,
    )
    empty = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=f"empty-{uuid.uuid4().hex[:8]}",
            prompt_template="t",
            agent_type="codex",
            agent_config={},
            callable_flows=[],
        ),
        account_id=test_user.account_id,
    )
    db_session.flush()
    db_session.expire_all()

    assert unset.callable_flows is None
    assert empty.callable_flows == []
    assert callable_flows_for(unset) == callable_flows_for(empty) == []
    assert parse_callable_flows(None) == parse_callable_flows([]) == []


def test_the_reader_helper_returns_parsed_entries(db_session, test_user):
    """A stored list comes back as entries, ceilings included."""
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=f"reader-{uuid.uuid4().hex[:8]}",
            prompt_template="t",
            agent_type="codex",
            agent_config={},
            callable_flows=[
                CallableFlowEntry(
                    flow="Child Flow", max_children=3, max_usd_per_child=2.5
                )
            ],
        ),
        account_id=test_user.account_id,
    )
    db_session.flush()
    db_session.expire_all()

    entries = callable_flows_for(flow)
    assert [entry.flow for entry in entries] == ["Child Flow"]
    assert entries[0].max_children == 3
    assert entries[0].max_usd_per_child == 2.5
    assert entries[0].allow_self is False


# --- clone and preset paths ------------------------------------------------


def test_cloning_a_flow_copies_the_allowlist(db_session, test_user, ai_model):
    """A preset may declare an allowlist and its clone has to carry it.

    Without this the clone is the one flow an operator cannot see is allowed to
    delegate, because the preset says one thing and the row says another.
    """
    preset = models.Flow(
        name=f"Delegating Preset {uuid.uuid4().hex[:8]}",
        prompt_template="t",
        agent_type="codex",
        agent_config={},
        allowed_mcp_servers=[],
        allowed_mcp_tools=[],
        is_preset=True,
        is_enabled=False,
        account_id=None,
        callable_flows=[{"flow": "Child Flow", "max_children": 2}],
    )
    db_session.add(preset)
    db_session.flush()

    clone = clone_preset_for_account(
        db_session,
        preset,
        test_user.account_id,
        bound_ai_model_id=ai_model.id,
    )
    db_session.flush()

    assert clone.id != preset.id
    assert clone.account_id is not None
    entries = callable_flows_for(clone)
    assert [(entry.flow, entry.max_children) for entry in entries] == [
        ("Child Flow", 2)
    ]
    # Stored as JSON on the clone's own row, not shared with the preset.
    assert isinstance(clone.callable_flows, list)
    assert clone.callable_flows[0]["flow"] == "Child Flow"
    assert clone.callable_flows[0]["max_children"] == 2

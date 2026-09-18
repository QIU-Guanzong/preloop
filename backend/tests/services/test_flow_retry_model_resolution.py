"""Tests for flow execution retry model and harness re-resolution.

Acceptance Criteria:
1. Change flow's model between original dispatch and retry -> retry's
   _model_routing.ai_model_id equals the new flow model.
2. Delete/retire originally routed model -> retry still dispatches on the flow
   model, with the reason recorded.
3. A matrix child retry keeps its index and agent_type override.
4. Matrix child user-supplied model override falls back to flow model if
   referenced row is deleted or no longer gateway-enabled, with note in new
   record.
5. Derived matrix overrides (not user-supplied) are stripped on retry so they
   re-resolve from the current flow.
6. Write fresh _model_routing on retry with source: "retry" and reason naming
   original execution ID.
"""

from unittest.mock import MagicMock, patch
from uuid import uuid4


from preloop.models.models.flow_execution import (
    MATRIX_OVERRIDES_KEY,
    ROUTING_RECORD_KEY,
)
from preloop.services.model_routing import (
    prepare_execution_routing,
)


def _mock_model(model_id, name="TestModel", gateway_enabled=True, account_id=None):
    model = MagicMock()
    model.id = model_id
    model.name = name
    model.model_identifier = f"provider/{name.lower()}"
    model.account_id = account_id
    model.provider_name = "openai"
    model.credentials_secret_id = "cred-1"
    model.api_key = None
    model.api_endpoint = None
    model.model_kind = "llm"
    model.meta_data = {"gateway": {"enabled": gateway_enabled}}
    return model


def _mock_flow(flow_id=None, account_id=None, model_id=None, agent_type="codex"):
    flow = MagicMock()
    flow.id = flow_id or uuid4()
    flow.account_id = account_id or uuid4()
    flow.agent_type = agent_type
    flow.ai_model_id = model_id or uuid4()
    flow.agent_config = {}
    return flow


def test_retry_reresolves_model_when_flow_model_changed():
    """1. Change flow's model between original dispatch and retry ->

    retry's _model_routing.ai_model_id equals the new flow model.
    6. Write fresh _model_routing on retry with source: 'retry' and reason naming original execution ID.
    """
    account_id = uuid4()
    old_model_id = uuid4()
    new_model_id = uuid4()

    old_model = _mock_model(old_model_id, name="OldModel", account_id=account_id)
    new_model = _mock_model(new_model_id, name="NewModel", account_id=account_id)

    flow = _mock_flow(account_id=account_id, model_id=new_model_id, agent_type="codex")

    prior_execution = MagicMock()
    prior_execution.id = uuid4()
    prior_execution.flow_id = flow.id
    prior_execution.trigger_event_details = {
        ROUTING_RECORD_KEY: {
            "schema_version": 1,
            "ai_model_id": str(old_model_id),
            "agent_type": "codex",
            "source": "default",
            "reason": "Flow default agent_type and model.",
            "label_snapshot": [],
        }
    }

    mock_db = MagicMock()

    def get_model(db, id=None):
        if str(id) == str(new_model_id):
            return new_model
        if str(id) == str(old_model_id):
            return old_model
        return None

    with patch(
        "preloop.services.model_routing.crud_ai_model.get", side_effect=get_model
    ):
        details = prepare_execution_routing(
            mock_db, flow, {}, source_execution=prior_execution, pin_kind="retry"
        )

    record = details[ROUTING_RECORD_KEY]
    assert record["source"] == "retry"
    assert record["ai_model_id"] == str(new_model_id)
    assert record["agent_type"] == "codex"
    assert str(prior_execution.id) in record["reason"]
    assert "Re-resolved model and harness from current flow" in record["reason"]


def test_retry_when_original_model_deleted():
    """2. Delete/retire originally routed model ->

    retry still dispatches on the flow model, with the reason recorded.
    """
    account_id = uuid4()
    retired_model_id = uuid4()
    current_flow_model_id = uuid4()

    current_model = _mock_model(
        current_flow_model_id, name="CurrentModel", account_id=account_id
    )

    flow = _mock_flow(
        account_id=account_id, model_id=current_flow_model_id, agent_type="codex"
    )

    prior_execution = MagicMock()
    prior_execution.id = uuid4()
    prior_execution.flow_id = flow.id
    prior_execution.trigger_event_details = {
        ROUTING_RECORD_KEY: {
            "schema_version": 1,
            "ai_model_id": str(retired_model_id),
            "agent_type": "codex",
            "source": "default",
            "reason": "Flow default agent_type and model.",
            "label_snapshot": [],
        }
    }

    mock_db = MagicMock()

    def get_model(db, id=None):
        if str(id) == str(current_flow_model_id):
            return current_model
        return None  # retired_model_id is deleted / retired

    with patch(
        "preloop.services.model_routing.crud_ai_model.get", side_effect=get_model
    ):
        details = prepare_execution_routing(
            mock_db, flow, {}, source_execution=prior_execution, pin_kind="retry"
        )

    record = details[ROUTING_RECORD_KEY]
    assert record["source"] == "retry"
    assert record["ai_model_id"] == str(current_flow_model_id)
    assert str(prior_execution.id) in record["reason"]
    assert str(retired_model_id) in record["reason"]
    assert "retired or unavailable" in record["reason"]


def test_matrix_child_retry_keeps_index_and_agent_type_override():
    """3. A matrix child retry keeps its index and agent_type override."""
    account_id = uuid4()
    model_id = uuid4()
    model = _mock_model(model_id, name="Model", account_id=account_id)

    flow = _mock_flow(account_id=account_id, model_id=model_id, agent_type="openhands")

    prior_execution = MagicMock()
    prior_execution.id = uuid4()
    prior_execution.flow_id = flow.id
    prior_execution.trigger_event_details = {
        MATRIX_OVERRIDES_KEY: {
            "batch_id": "batch-123",
            "index": 4,
            "agent_type": "opencode",  # override flow's openhands
            "ai_model_id": str(model_id),
        },
        ROUTING_RECORD_KEY: {
            "schema_version": 1,
            "ai_model_id": str(model_id),
            "agent_type": "opencode",
            "source": "matrix",
        },
    }

    mock_db = MagicMock()
    with patch("preloop.services.model_routing.crud_ai_model.get", return_value=model):
        details = prepare_execution_routing(
            mock_db, flow, {}, source_execution=prior_execution, pin_kind="retry"
        )

    matrix = details[MATRIX_OVERRIDES_KEY]
    assert matrix["index"] == 4
    assert matrix["batch_id"] == "batch-123"
    assert matrix["agent_type"] == "opencode"
    assert matrix["ai_model_id"] == str(model_id)

    record = details[ROUTING_RECORD_KEY]
    assert record["source"] == "retry"
    assert str(prior_execution.id) in record["reason"]


def test_matrix_child_falls_back_when_user_model_deleted_or_gateway_disabled():
    """4. Matrix child user-supplied model override falls back to flow model if

    referenced row is deleted or no longer gateway-enabled, with note in new
    record.
    """
    account_id = uuid4()
    flow_model_id = uuid4()
    deleted_model_id = uuid4()
    disabled_gateway_model_id = uuid4()

    flow_model = _mock_model(flow_model_id, name="FlowModel", account_id=account_id)
    disabled_model = _mock_model(
        disabled_gateway_model_id,
        name="DisabledModel",
        account_id=account_id,
        gateway_enabled=False,
    )

    flow = _mock_flow(account_id=account_id, model_id=flow_model_id, agent_type="codex")

    mock_db = MagicMock()

    def get_model(db, id=None):
        if str(id) == str(flow_model_id):
            return flow_model
        if str(id) == str(disabled_gateway_model_id):
            return disabled_model
        return None  # deleted_model_id

    # Test 4a: user model was deleted
    prior_deleted = MagicMock()
    prior_deleted.id = uuid4()
    prior_deleted.flow_id = flow.id
    prior_deleted.trigger_event_details = {
        MATRIX_OVERRIDES_KEY: {
            "batch_id": "batch-1",
            "index": 1,
            "agent_type": "opencode",
            "ai_model_id": str(deleted_model_id),
        },
        ROUTING_RECORD_KEY: {
            "schema_version": 1,
            "ai_model_id": str(deleted_model_id),
            "agent_type": "opencode",
            "source": "matrix",
        },
    }

    with patch(
        "preloop.services.model_routing.crud_ai_model.get", side_effect=get_model
    ):
        details = prepare_execution_routing(
            mock_db, flow, {}, source_execution=prior_deleted, pin_kind="retry"
        )

    matrix = details[MATRIX_OVERRIDES_KEY]
    assert matrix["ai_model_id"] == str(flow_model_id)
    record = details[ROUTING_RECORD_KEY]
    assert record["source"] == "retry"
    assert (
        "unavailable or gateway-disabled; fell back to flow model" in record["reason"]
    )

    # Test 4b: user model is no longer gateway-enabled
    prior_disabled = MagicMock()
    prior_disabled.id = uuid4()
    prior_disabled.flow_id = flow.id
    prior_disabled.trigger_event_details = {
        MATRIX_OVERRIDES_KEY: {
            "batch_id": "batch-2",
            "index": 2,
            "agent_type": "opencode",
            "ai_model_id": str(disabled_gateway_model_id),
        },
        ROUTING_RECORD_KEY: {
            "schema_version": 1,
            "ai_model_id": str(disabled_gateway_model_id),
            "agent_type": "opencode",
            "source": "matrix",
        },
    }

    with patch(
        "preloop.services.model_routing.crud_ai_model.get", side_effect=get_model
    ):
        details2 = prepare_execution_routing(
            mock_db, flow, {}, source_execution=prior_disabled, pin_kind="retry"
        )

    matrix2 = details2[MATRIX_OVERRIDES_KEY]
    assert matrix2["ai_model_id"] == str(flow_model_id)
    record2 = details2[ROUTING_RECORD_KEY]
    assert record2["source"] == "retry"
    assert (
        "unavailable or gateway-disabled; fell back to flow model" in record2["reason"]
    )


def test_matrix_child_derived_overrides_stripped_and_reresolved():
    """5. Derived matrix overrides (not user-supplied) are stripped on retry so

    they re-resolve from the current flow.
    """
    account_id = uuid4()
    old_flow_model_id = uuid4()
    new_flow_model_id = uuid4()

    new_flow_model = _mock_model(
        new_flow_model_id, name="NewFlowModel", account_id=account_id
    )

    # Initial flow had old_flow_model_id and agent_type="openhands"
    # An authorized matrix cell with only index: 0 had its model derived
    flow_init = _mock_flow(
        account_id=account_id, model_id=old_flow_model_id, agent_type="openhands"
    )
    mock_db = MagicMock()

    with patch(
        "preloop.services.model_routing.crud_ai_model.get",
        return_value=_mock_model(old_flow_model_id, account_id=account_id),
    ):
        initial_details = prepare_execution_routing(
            mock_db, flow_init, {}, authorized_matrix={"index": 0}
        )

    matrix_cell = initial_details[MATRIX_OVERRIDES_KEY]
    assert matrix_cell["ai_model_id"] == str(old_flow_model_id)
    assert "ai_model_id" in matrix_cell.get("derived", [])

    # Now flow is updated with new model and agent_type="codex"
    flow_updated = _mock_flow(
        flow_id=flow_init.id,
        account_id=account_id,
        model_id=new_flow_model_id,
        agent_type="codex",
    )

    prior_execution = MagicMock()
    prior_execution.id = uuid4()
    prior_execution.flow_id = flow_updated.id
    prior_execution.trigger_event_details = initial_details

    with patch(
        "preloop.services.model_routing.crud_ai_model.get", return_value=new_flow_model
    ):
        retry_details = prepare_execution_routing(
            mock_db,
            flow_updated,
            {},
            source_execution=prior_execution,
            pin_kind="retry",
        )

    retried_matrix = retry_details[MATRIX_OVERRIDES_KEY]
    # Since ai_model_id and agent_type were derived, they stripped and re-resolved from updated flow!
    assert retried_matrix["index"] == 0
    assert retried_matrix["agent_type"] == "codex"
    assert retried_matrix["ai_model_id"] == str(new_flow_model_id)
    assert retry_details[ROUTING_RECORD_KEY]["source"] == "retry"

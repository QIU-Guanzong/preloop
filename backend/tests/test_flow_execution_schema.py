"""Tests for flow execution Pydantic schemas."""

import uuid
from datetime import datetime

import pytest
from pydantic import ValidationError

from preloop.schemas.flow_execution import (
    FlowExecutionBase,
    FlowExecutionDetailResponse,
    FlowExecutionListResponse,
)


class TestFlowExecutionBase:
    """Test FlowExecutionBase schema."""

    def test_create_with_required_fields(self):
        """Test creating FlowExecutionBase with required fields only."""
        execution_id = uuid.uuid4()
        flow_id = uuid.uuid4()
        start_time = datetime.now()
        created_at = datetime.now()
        updated_at = datetime.now()

        execution = FlowExecutionBase(
            id=execution_id,
            flow_id=flow_id,
            status="PENDING",
            start_time=start_time,
            created_at=created_at,
            updated_at=updated_at,
        )

        assert execution.id == execution_id
        assert execution.flow_id == flow_id
        assert execution.status == "PENDING"
        assert execution.start_time == start_time
        assert execution.trigger_event_id is None
        assert execution.trigger_event_details is None
        assert execution.end_time is None

    def test_create_with_all_fields(self):
        """Test creating FlowExecutionBase with all optional fields."""
        execution_id = uuid.uuid4()
        flow_id = uuid.uuid4()
        start_time = datetime.now()
        end_time = datetime.now()
        created_at = datetime.now()
        updated_at = datetime.now()

        trigger_details = {"issue_key": "PROJ-123", "action": "created"}
        mcp_logs = [
            {"tool": "create_issue", "timestamp": "2025-01-15T10:00:00Z"},
            {"tool": "update_issue", "timestamp": "2025-01-15T10:05:00Z"},
        ]
        actions = [
            {"action": "created_issue", "result": "PROJ-456"},
            {"action": "added_comment", "result": "success"},
        ]

        execution = FlowExecutionBase(
            id=execution_id,
            flow_id=flow_id,
            trigger_event_id="event-12345",
            trigger_event_details=trigger_details,
            status="SUCCEEDED",
            start_time=start_time,
            end_time=end_time,
            resolved_input_prompt="Create issue with title: Test",
            model_output_summary="Successfully created issue PROJ-456",
            actions_taken_summary=actions,
            mcp_usage_logs=mcp_logs,
            openhands_session_reference="k8s-job-abc123",
            error_message=None,
            created_at=created_at,
            updated_at=updated_at,
        )

        assert execution.trigger_event_id == "event-12345"
        assert execution.trigger_event_details == trigger_details
        assert execution.status == "SUCCEEDED"
        assert execution.end_time == end_time
        assert execution.resolved_input_prompt == "Create issue with title: Test"
        assert execution.model_output_summary == "Successfully created issue PROJ-456"
        assert execution.actions_taken_summary == actions
        assert execution.mcp_usage_logs == mcp_logs
        assert execution.openhands_session_reference == "k8s-job-abc123"
        assert execution.error_message is None

    def test_create_with_failed_status(self):
        """Test creating FlowExecutionBase with FAILED status and error."""
        execution_id = uuid.uuid4()
        flow_id = uuid.uuid4()
        start_time = datetime.now()
        end_time = datetime.now()
        created_at = datetime.now()
        updated_at = datetime.now()

        execution = FlowExecutionBase(
            id=execution_id,
            flow_id=flow_id,
            status="FAILED",
            start_time=start_time,
            end_time=end_time,
            error_message="Failed to connect to Jira API: Connection timeout",
            created_at=created_at,
            updated_at=updated_at,
        )

        assert execution.status == "FAILED"
        assert (
            execution.error_message
            == "Failed to connect to Jira API: Connection timeout"
        )

    def test_required_fields_validation(self):
        """Test that required fields are validated."""
        with pytest.raises(ValidationError) as exc_info:
            FlowExecutionBase()

        errors = exc_info.value.errors()
        error_fields = {error["loc"][0] for error in errors}

        # Check that required fields are in the error
        required_fields = {
            "id",
            "flow_id",
            "status",
            "start_time",
            "created_at",
            "updated_at",
        }
        assert required_fields.issubset(error_fields)

    def test_from_attributes_config(self):
        """Test that from_attributes is enabled in Config."""
        # Check both old and new style config
        assert (
            hasattr(FlowExecutionBase, "Config")
            and FlowExecutionBase.Config.from_attributes is True
        ) or (
            hasattr(FlowExecutionBase, "model_config")
            and FlowExecutionBase.model_config.get("from_attributes") is True
        )


class TestFlowExecutionListResponse:
    """Test FlowExecutionListResponse schema."""

    def test_create_minimal_list_response(self):
        """Test creating minimal list response."""
        execution_id = uuid.uuid4()
        flow_id = uuid.uuid4()
        start_time = datetime.now()
        created_at = datetime.now()

        response = FlowExecutionListResponse(
            id=execution_id,
            flow_id=flow_id,
            status="RUNNING",
            start_time=start_time,
            created_at=created_at,
        )

        assert response.id == execution_id
        assert response.flow_id == flow_id
        assert response.status == "RUNNING"
        assert response.start_time == start_time
        assert response.end_time is None
        assert response.created_at == created_at

    def test_create_complete_list_response(self):
        """Test creating complete list response with end_time."""
        execution_id = uuid.uuid4()
        flow_id = uuid.uuid4()
        start_time = datetime.now()
        end_time = datetime.now()
        created_at = datetime.now()

        response = FlowExecutionListResponse(
            id=execution_id,
            flow_id=flow_id,
            status="SUCCEEDED",
            start_time=start_time,
            end_time=end_time,
            created_at=created_at,
        )

        assert response.status == "SUCCEEDED"
        assert response.end_time == end_time

    def test_list_response_subset_of_base(self):
        """Test that list response is a subset of base fields."""
        # List response should have fewer fields than base
        base_fields = set(FlowExecutionBase.model_fields.keys())
        list_fields = set(FlowExecutionListResponse.model_fields.keys())

        assert list_fields.issubset(base_fields)
        assert len(list_fields) < len(base_fields)

    def test_from_attributes_config(self):
        """Test that from_attributes is enabled in Config."""
        assert (
            hasattr(FlowExecutionListResponse, "Config")
            and FlowExecutionListResponse.Config.from_attributes is True
        ) or (
            hasattr(FlowExecutionListResponse, "model_config")
            and FlowExecutionListResponse.model_config.get("from_attributes") is True
        )


class TestFlowExecutionDetailResponse:
    """Test FlowExecutionDetailResponse schema."""

    def test_inherits_from_base(self):
        """Test that detail response inherits from base."""
        assert issubclass(FlowExecutionDetailResponse, FlowExecutionBase)

    def test_create_detail_response(self):
        """Test creating detail response with all base fields."""
        execution_id = uuid.uuid4()
        flow_id = uuid.uuid4()
        start_time = datetime.now()
        end_time = datetime.now()
        created_at = datetime.now()
        updated_at = datetime.now()

        response = FlowExecutionDetailResponse(
            id=execution_id,
            flow_id=flow_id,
            trigger_event_id="event-67890",
            trigger_event_details={"source": "webhook"},
            status="SUCCEEDED",
            start_time=start_time,
            end_time=end_time,
            resolved_input_prompt="Test prompt",
            model_output_summary="Task completed",
            actions_taken_summary=[{"action": "test"}],
            mcp_usage_logs=[{"tool": "test_tool"}],
            openhands_session_reference="session-xyz",
            error_message=None,
            created_at=created_at,
            updated_at=updated_at,
        )

        # Should have all base fields
        assert response.id == execution_id
        assert response.trigger_event_id == "event-67890"
        assert response.resolved_input_prompt == "Test prompt"
        assert response.model_output_summary == "Task completed"

    def test_has_same_fields_as_base(self):
        """Test that detail response has the same fields as base."""
        base_fields = set(FlowExecutionBase.model_fields.keys())
        detail_fields = set(FlowExecutionDetailResponse.model_fields.keys())

        # Detail response should have at least all base fields
        assert base_fields.issubset(detail_fields)


class TestExecutionLineageFields:
    """The delegation-tree columns are readable from the execution schemas.

    A console (or an operator with curl) has to be able to tell a root run
    from a delegated child without walking logs, so the lineage has to survive
    serialization on the response the detail endpoint returns.
    """

    def test_legacy_response_schema_exposes_lineage_with_root_defaults(self):
        """Detail response carries lineage and defaults an existing row to a root."""
        fields = FlowExecutionDetailResponse.model_fields

        assert "parent_execution_id" in fields
        assert "root_execution_id" in fields
        assert "delegation_depth" in fields
        assert fields["parent_execution_id"].default is None
        assert fields["root_execution_id"].default is None
        assert fields["delegation_depth"].default == 0

    def test_api_response_schema_exposes_lineage_with_root_defaults(self):
        """The endpoint's response schema does the same, for old and new rows."""
        from preloop.models.schemas.flow_execution import FlowExecutionResponse

        fields = FlowExecutionResponse.model_fields
        assert "parent_execution_id" in fields
        assert "root_execution_id" in fields
        assert "delegation_depth" in fields
        assert fields["parent_execution_id"].default is None
        assert fields["root_execution_id"].default is None
        assert fields["delegation_depth"].default == 0

        now = datetime.now()
        response = FlowExecutionResponse.model_validate(
            {
                "id": uuid.uuid4(),
                "flow_id": uuid.uuid4(),
                "status": "SUCCEEDED",
                "start_time": now,
                "created_at": now,
                "updated_at": now,
            }
        )
        assert response.parent_execution_id is None
        assert response.root_execution_id is None
        assert response.delegation_depth == 0

    def test_creation_schema_accepts_lineage(self):
        """Creation is the only writer: the create schema must carry the fields."""
        from preloop.models.schemas.flow_execution import FlowExecutionCreate

        fields = FlowExecutionCreate.model_fields
        assert {"parent_execution_id", "root_execution_id", "delegation_depth"} <= set(
            fields
        )

"""Ephemeral harness approval credentials cannot escape their execution session."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.api.endpoints import agent_permission as endpoint


@pytest.mark.parametrize(
    "scenario",
    ["active", "ended", "different_execution", "ordinary_key", "missing_session"],
)
def test_flow_permission_requires_own_active_execution(
    monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    execution_id, session_id, account_id = uuid4(), uuid4(), uuid4()
    key = SimpleNamespace(
        id=uuid4(),
        account_id=account_id,
        context_data={"flow_execution_id": str(execution_id)},
    )
    session = SimpleNamespace(
        id=session_id,
        session_source_type="flow_execution",
        session_source_id=str(execution_id),
        ended_at=None,
        runtime_principal_name="Test flow",
    )
    if scenario == "ended":
        session.ended_at = "2026-01-01"
    elif scenario == "different_execution":
        session.session_source_id = str(uuid4())
    elif scenario == "ordinary_key":
        key.context_data = {}
    elif scenario == "missing_session":
        session = None
    factory = MagicMock()
    monkeypatch.setattr(endpoint, "get_session_factory", lambda: factory)
    monkeypatch.setattr(
        endpoint.crud_api_key, "get_by_key", lambda *args, **kwargs: key
    )
    monkeypatch.setattr(
        endpoint,
        "_authenticate_with_api_key",
        lambda *args: SimpleNamespace(id=uuid4()),
    )
    monkeypatch.setattr(endpoint, "_managed_agent_for_api_key", lambda *args: None)
    monkeypatch.setattr(
        endpoint, "_runtime_session_id_from_api_key", lambda *args: session_id
    )
    monkeypatch.setattr(
        endpoint.crud_runtime_session,
        "get_account_session",
        lambda *args, **kwargs: session,
    )
    if scenario == "active":
        identity = endpoint._resolve_permission_identity("flow-token")
        assert identity.managed_agent_id is None
        assert identity.runtime_session_id == session_id
        assert identity.managed_agent_name == "Test flow"
    else:
        with pytest.raises(HTTPException) as exc:
            endpoint._resolve_permission_identity("flow-token")
        assert exc.value.status_code == 401

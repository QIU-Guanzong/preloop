"""Pi and DeepSeek enroll through the same API used by the CLI."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_runtime_session
from preloop.api.endpoints.account import _managed_agent_control_fields


@pytest.mark.parametrize("kind,label", [("pi", "Pi"), ("deepseek", "DeepSeek Harness")])
def test_harness_enrollment_is_idempotent_and_filterable(
    client: TestClient,
    db_session: Session,
    test_user: models.User,
    kind: str,
    label: str,
) -> None:
    payload = {
        "session_source_type": kind,
        "session_source_id": f"{kind}-test-workstation",
        "runtime_principal_id": f"{kind}-test-workstation",
        "runtime_principal_name": label,
        "agent_kind": kind,
    }
    for _ in range(2):
        enrolled = client.post("/api/v1/auth/runtime-sessions/token", json=payload)
        assert enrolled.status_code == 201, enrolled.text
    response = client.get(f"/api/v1/agents?agent_kind={kind}")
    assert response.status_code == 200, response.text
    agents = response.json()["items"]
    assert len(agents) == 1
    assert agents[0]["agent_kind"] == kind
    assert agents[0]["session_source_type"] == kind
    assert agents[0]["display_name"] == label
    fields = _managed_agent_control_fields(
        agents[0],
        {"validation_result": {"control_channel_configured": True}},
        ws_connected=True,
    )
    assert fields["control_enabled"] is True
    assert fields["supports_interrupt"] is True
    assert fields["supports_new_session"] is False
    assert fields["supports_voice"] is False
    assert "start_new_session" not in fields["control_capabilities"]
    assert "release" not in fields["control_capabilities"]
    rejected = client.post(
        f"/api/v1/agents/{agents[0]['id']}/control/commands",
        json={
            "message": "Start over",
            "start_new_session": True,
        },
    )
    assert rejected.status_code == 400, rejected.text
    for event in ("session_start", "response", "session_end"):
        ingested = client.post(
            "/api/v1/usage/ingest",
            json={
                "source": kind,
                "agent_id": agents[0]["id"],
                "records": [
                    {
                        "external_id": f"{kind}-{event}",
                        "conversation_id": "native-session",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event_type": event,
                    }
                ],
            },
        )
        assert ingested.status_code == 200, ingested.text
        assert ingested.json()["accepted"] == 1
    session = crud_runtime_session.get_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type=kind,
        session_source_id="native-session",
    )
    assert session is not None
    assert session.runtime_principal_id == payload["runtime_principal_id"]
    assert session.ended_at is not None


@pytest.mark.parametrize("kind", ["pi", "deepseek"])
@pytest.mark.parametrize("interrupt", [False, True])
def test_harness_interrupt_honors_runtime_presence(
    kind: str, interrupt: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "preloop.api.endpoints.agent_control.agent_control_snapshot",
        lambda _id: {"online": True, "supports_interrupt": interrupt},
    )
    fields = _managed_agent_control_fields(
        {"id": "test-agent", "agent_kind": kind, "lifecycle_state": "active"},
        {"validation_result": {"control_channel_configured": True}},
    )
    assert fields["control_online"] is True
    assert fields["supports_interrupt"] is interrupt
    assert ("interrupt" in fields["control_capabilities"]) is interrupt

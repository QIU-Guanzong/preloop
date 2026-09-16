"""Registration, hello and unregister for one-shot CI runners."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import runners
from preloop.models import models
from preloop.models.crud.flow_runner import ONLINE_HEARTBEAT_TTL, crud_flow_runner
from preloop.models.db.session import get_db_session as get_db


def _client(db_session: Session, test_user: models.User) -> TestClient:
    app = FastAPI()
    app.include_router(runners.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_user
    return TestClient(app)


def test_register_marks_the_row_ephemeral(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *args: None)
    with _client(db_session, test_user) as client:
        response = client.post(
            "/api/v1/runners/register",
            json={"name": "ci-job-1", "labels": ["ci-gha-42"], "ephemeral": True},
        )
    assert response.status_code == 200, response.text
    assert response.json()["ephemeral"] is True
    saved = crud_flow_runner.get_fresh(
        db_session, runner_id=UUID(response.json()["id"])
    )
    assert saved is not None and saved.ephemeral is True


def test_register_reaps_a_previous_job_that_never_unregistered(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A killed CI job leaves a row; the next registration clears it."""
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *args: None)
    abandoned = crud_flow_runner.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "name": "ci-job-killed",
            "token_hash": f"hash-{uuid4()}",
            "ephemeral": True,
            "status": "online",
            "last_heartbeat": datetime.now(timezone.utc)
            - ONLINE_HEARTBEAT_TTL
            - timedelta(seconds=5),
        },
    )
    abandoned_id = abandoned.id

    with _client(db_session, test_user) as client:
        response = client.post(
            "/api/v1/runners/register", json={"name": "ci-job-2", "ephemeral": True}
        )
    assert response.status_code == 200, response.text
    assert crud_flow_runner.get_fresh(db_session, runner_id=abandoned_id) is None


def test_register_defaults_to_a_persistent_runner(
    db_session: Session, test_user: models.User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runners, "emit_runner_updated", lambda *args: None)
    with _client(db_session, test_user) as client:
        response = client.post("/api/v1/runners/register", json={"name": "desk-mac"})
    assert response.status_code == 200, response.text
    assert response.json()["ephemeral"] is False


def _capability_writer(runner: SimpleNamespace):
    """Stand-in for the compare-and-swap that stamps the connection id."""

    def set_publication_capabilities(db, *, capabilities=None, **kwargs) -> bool:
        runner.publication_capabilities = capabilities
        return True

    return set_publication_capabilities


def _ws_runner(ephemeral: bool) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        current_execution_id=None,
        pending_job=None,
        halt_requested=False,
        status="online",
        reported_status=None,
        publication_capabilities=None,
        ephemeral=ephemeral,
        capabilities={},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("ephemeral", [True, False])
async def test_hello_reports_whether_the_row_is_ephemeral(
    monkeypatch: pytest.MonkeyPatch, ephemeral: bool
) -> None:
    runner = _ws_runner(ephemeral)
    websocket = MagicMock()
    websocket.accept = AsyncMock()
    websocket.send_json = AsyncMock()
    websocket.receive_json = AsyncMock(side_effect=[WebSocketDisconnect()])
    monkeypatch.setattr(runners, "_authenticate_runner", lambda *args: runner)
    monkeypatch.setattr(runners, "emit_runner_updated", MagicMock())
    monkeypatch.setattr(runners.crud_flow_runner, "get", lambda *args, **kwargs: runner)
    monkeypatch.setattr(runners.crud_flow_runner, "touch_heartbeat", MagicMock())
    monkeypatch.setattr(
        runners.crud_flow_runner,
        "set_publication_capabilities",
        _capability_writer(runner),
    )

    await runners.runner_ws(websocket, runner.id, MagicMock())

    hello = websocket.send_json.await_args_list[0].args[0]
    assert hello["type"] == "hello"
    assert hello["ephemeral"] is ephemeral


@pytest.mark.asyncio
async def test_heartbeat_upgrades_a_row_the_register_missed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI re-asserts ephemeral so a resumed row cannot outlive its job."""
    runner = _ws_runner(False)
    websocket = MagicMock()
    websocket.accept = AsyncMock()
    websocket.send_json = AsyncMock()
    websocket.receive_json = AsyncMock(
        side_effect=[{"type": "heartbeat", "ephemeral": True}, WebSocketDisconnect()]
    )
    mark = MagicMock(return_value=True)
    monkeypatch.setattr(runners, "_authenticate_runner", lambda *args: runner)
    monkeypatch.setattr(runners, "emit_runner_updated", MagicMock())
    monkeypatch.setattr(runners.crud_flow_runner, "get", lambda *args, **kwargs: runner)
    monkeypatch.setattr(runners.crud_flow_runner, "touch_heartbeat", MagicMock())
    monkeypatch.setattr(runners.crud_flow_runner, "mark_ephemeral", mark)
    monkeypatch.setattr(
        runners.crud_flow_runner,
        "set_publication_capabilities",
        _capability_writer(runner),
    )

    await runners.runner_ws(websocket, runner.id, MagicMock())

    mark.assert_called_once()
    assert mark.call_args.kwargs["runner_id"] == runner.id


@pytest.mark.asyncio
async def test_unregister_deletes_an_ephemeral_row_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _ws_runner(True)
    websocket = MagicMock()
    websocket.accept = AsyncMock()
    websocket.send_json = AsyncMock()
    websocket.receive_json = AsyncMock(
        side_effect=[{"type": "unregister"}, WebSocketDisconnect()]
    )
    sweep = MagicMock(return_value=1)
    monkeypatch.setattr(runners, "_authenticate_runner", lambda *args: runner)
    monkeypatch.setattr(runners, "emit_runner_updated", MagicMock())
    monkeypatch.setattr(runners.crud_flow_runner, "get", lambda *args, **kwargs: runner)
    monkeypatch.setattr(
        runners.crud_flow_runner, "get_fresh", lambda *args, **kwargs: runner
    )
    monkeypatch.setattr(runners.crud_flow_runner, "touch_heartbeat", MagicMock())
    monkeypatch.setattr(runners.crud_flow_runner, "sweep_stale_ephemeral", sweep)
    monkeypatch.setattr(
        runners.crud_flow_runner,
        "set_publication_capabilities",
        _capability_writer(runner),
    )

    await runners.runner_ws(websocket, runner.id, MagicMock())

    sweep.assert_called_once()
    assert sweep.call_args.kwargs["grace"] == timedelta(0)
    assert sweep.call_args.kwargs["account_id"] == runner.account_id
    # The runner still gets its acknowledgement before the socket closes.
    assert websocket.send_json.await_args_list[-1].args[0] == {"type": "ack"}

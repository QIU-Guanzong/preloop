"""The runner control socket must not idle inside a database transaction.

A private runner heartbeats every few seconds and says nothing in between. The
handler used to answer a heartbeat, re-read the runner and its assignments, and
then block on the socket with that read transaction still open, so the session
sat "idle in transaction" holding AccessShareLock on `flow_runner`,
`flow_runner_assignment` and `flow_execution` for the whole quiet gap. A
schema migration that wants ACCESS EXCLUSIVE on one of those tables queues
behind it, and every query that arrives after the migration queues behind the
migration: one silent runner is enough to stall a deployment.
"""

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect

from preloop.api.endpoints import runners
from preloop.models import models
from preloop.models.crud import crud_account
from preloop.models.crud.flow_runner import crud_flow_runner


@pytest.fixture
def connected_runner(db_session: Session) -> models.FlowRunner:
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Runner transaction test"}
    )
    runner = crud_flow_runner.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "name": "idle runner",
            "token_hash": "test-token",
            "status": "online",
            "concurrency": 1,
        },
    )
    db_session.commit()
    return runner


def _socket(
    monkeypatch: pytest.MonkeyPatch,
    runner: models.FlowRunner,
    db_session: Session,
    messages: List[Dict[str, Any]],
    in_transaction_at_receive: List[bool],
    in_transaction_at_send: List[bool] | None = None,
) -> MagicMock:
    """A socket that records whether a transaction is open while it waits."""
    queued = list(messages)

    async def receive_json() -> Dict[str, Any]:
        # This is the moment that matters: the handler is about to wait for a
        # message it may not get for another fifteen seconds.
        in_transaction_at_receive.append(db_session.in_transaction())
        if not queued:
            raise WebSocketDisconnect()
        return queued.pop(0)

    async def send_json(payload: Dict[str, Any]) -> None:
        # Sending waits too: a connected runner that has stopped reading
        # applies backpressure and parks the handler right here.
        if in_transaction_at_send is not None:
            in_transaction_at_send.append(db_session.in_transaction())

    websocket = MagicMock()
    websocket.accept = AsyncMock()
    websocket.send_json = AsyncMock(side_effect=send_json)
    websocket.receive_json = AsyncMock(side_effect=receive_json)
    monkeypatch.setattr(runners, "_authenticate_runner", lambda *args: runner)
    monkeypatch.setattr(runners, "emit_runner_updated", MagicMock())
    return websocket


@pytest.mark.asyncio
async def test_heartbeat_leaves_no_open_transaction(
    db_session: Session,
    connected_runner: models.FlowRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: List[bool] = []
    websocket = _socket(
        monkeypatch,
        connected_runner,
        db_session,
        [{"type": "heartbeat", "concurrency": 1}],
        observed,
    )

    await runners.runner_ws(websocket, connected_runner.id, db_session)

    # One wait before the heartbeat (after the hello handshake wrote the
    # connection id) and one after it was answered. Neither may hold locks.
    assert len(observed) == 2
    assert observed == [False, False]


@pytest.mark.asyncio
async def test_repeated_heartbeats_stay_out_of_transactions(
    db_session: Session,
    connected_runner: models.FlowRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock is released on every quiet gap, not only the first one."""
    observed: List[bool] = []
    websocket = _socket(
        monkeypatch,
        connected_runner,
        db_session,
        [{"type": "heartbeat", "concurrency": 1} for _ in range(3)],
        observed,
    )

    await runners.runner_ws(websocket, connected_runner.id, db_session)

    assert len(observed) == 4
    assert not any(observed)


@pytest.mark.asyncio
async def test_the_hello_and_ack_sends_hold_no_locks_either(
    db_session: Session,
    connected_runner: models.FlowRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner that is connected but not reading must not park a lock.

    The hello payload and the heartbeat ack are both built from reads
    (`db.refresh`, the assignment replay), and both are then written to a peer
    that controls how long the write takes. Same failure as the receive gap,
    with a rarer trigger.
    """
    observed_receive: List[bool] = []
    observed_send: List[bool] = []
    websocket = _socket(
        monkeypatch,
        connected_runner,
        db_session,
        [{"type": "heartbeat", "concurrency": 2}],
        observed_receive,
        observed_send,
    )

    await runners.runner_ws(websocket, connected_runner.id, db_session)

    # The hello and the ack.
    assert len(observed_send) == 2
    assert not any(observed_send)
    assert not any(observed_receive)


@pytest.mark.asyncio
async def test_a_status_message_still_persists_before_the_wait(
    db_session: Session,
    connected_runner: models.FlowRunner,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Releasing the transaction must commit the work, not discard it."""
    observed: List[bool] = []
    websocket = _socket(
        monkeypatch,
        connected_runner,
        db_session,
        [{"type": "heartbeat", "concurrency": 4}],
        observed,
    )

    await runners.runner_ws(websocket, connected_runner.id, db_session)

    db_session.expire_all()
    saved = crud_flow_runner.get(db_session, id=connected_runner.id)
    assert saved is not None
    assert saved.reported_concurrency == 4
    assert not any(observed)

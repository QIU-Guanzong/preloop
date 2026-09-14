"""Flow-execution workers may babysit several hosted monitors at once."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import nats
import pytest

from preloop.sync.services.nats_worker import (
    FLOW_ORCHESTRATION_TASKS,
    PreloopSyncNatsWorker,
)


class _FakeSub:
    def __init__(self, payloads: list[bytes]) -> None:
        self._payloads = list(payloads)
        self.subject = "preloop.sync.tasks.execute_flow"

    async def fetch(self, batch: int = 1, timeout: int = 60) -> list[Any]:
        if not self._payloads:
            await asyncio.sleep(0)
            raise nats.errors.TimeoutError()
        data = self._payloads.pop(0)
        msg = MagicMock()
        msg.data = data
        msg.nak = AsyncMock()
        return [msg]


@pytest.mark.asyncio
async def test_flow_worker_runs_monitors_in_parallel() -> None:
    """Ten slots are available; a cap of 3 must not start a fourth until one ends."""
    started = asyncio.Event()
    release = asyncio.Event()
    inflight = 0
    peak = 0

    async def handler(_msg: Any) -> None:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        started.set()
        await release.wait()
        inflight -= 1

    worker = PreloopSyncNatsWorker(
        "nats://example.invalid:4222",
        "flow-pool",
        tasks_allowlist=list(FLOW_ORCHESTRATION_TASKS),
        max_inflight=3,
    )
    worker._run_slots = asyncio.Semaphore(worker.handler_concurrency())
    payloads = [b'{"function":"execute_flow"}' for _ in range(6)]
    sub = _FakeSub(payloads)
    pull = asyncio.create_task(worker._process_pull_messages(sub, handler))
    deadline = asyncio.get_running_loop().time() + 1
    while inflight < 3 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert peak == 3
    assert inflight == 3
    release.set()
    worker._accepting = False
    await asyncio.wait_for(pull, timeout=2)
    assert peak == 3


@pytest.mark.asyncio
async def test_non_flow_worker_stays_serial() -> None:
    order: list[str] = []

    async def handler(_msg: Any) -> None:
        order.append("start")
        await asyncio.sleep(0.02)
        order.append("end")

    worker = PreloopSyncNatsWorker(
        "nats://example.invalid:4222",
        "webhook-pool",
        tasks_allowlist=["process_webhook_event"],
    )
    assert worker.handler_concurrency() == 1
    worker._run_slots = asyncio.Semaphore(1)
    sub = _FakeSub([b"{}", b'{"function":"x"}'])
    pull = asyncio.create_task(worker._process_pull_messages(sub, handler))
    deadline = asyncio.get_running_loop().time() + 1
    while order.count("end") < 2 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    worker._accepting = False
    await asyncio.wait_for(pull, timeout=2)
    assert order[:4] == ["start", "end", "start", "end"]


def test_default_flow_inflight_is_ten() -> None:
    worker = PreloopSyncNatsWorker(
        "nats://example.invalid:4222",
        "flow-pool",
        tasks_allowlist=list(FLOW_ORCHESTRATION_TASKS),
    )
    assert worker.handler_concurrency() == 10


def test_exclude_only_pool_stays_serial() -> None:
    """Helm default pool excludes flow tasks; it must not inherit fan-out."""
    worker = PreloopSyncNatsWorker(
        "nats://example.invalid:4222",
        "default-pool",
        tasks_excludelist=["execute_flow", "resume_flow_execution"],
    )
    assert worker.handler_concurrency() == 1
    assert worker.handles_flow_orchestration is True


def test_catch_all_worker_uses_flow_inflight() -> None:
    worker = PreloopSyncNatsWorker(
        "nats://example.invalid:4222",
        "all-tasks",
    )
    assert worker.handler_concurrency() == 10

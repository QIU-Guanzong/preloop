"""What the stale-claim reaper asks before it publishes anything.

The reaper pass is leased, so one replica runs it for the whole pool. That
replica's own semaphore says nothing about its peers, so the answer comes
from JetStream: flow tasks still waiting to be delivered mean nobody has a
free slot, and publishing more of them is load with no progress behind it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from preloop.sync.services.nats_worker import (
    FLOW_ORCHESTRATION_TASKS,
    PreloopSyncNatsWorker,
)


def _worker() -> PreloopSyncNatsWorker:
    return PreloopSyncNatsWorker(
        "nats://example.invalid:4222",
        "flow-pool",
        tasks_allowlist=list(FLOW_ORCHESTRATION_TASKS),
    )


def _sub(num_pending: int | None = 0, *, failing: bool = False):
    sub = MagicMock()
    if failing:
        sub.consumer_info = AsyncMock(side_effect=RuntimeError("no connection"))
        return sub
    info = MagicMock()
    info.num_pending = num_pending
    sub.consumer_info = AsyncMock(return_value=info)
    return sub


@pytest.mark.asyncio
async def test_an_empty_queue_means_there_is_capacity() -> None:
    worker = _worker()
    worker.subs = [("preloop.sync.tasks.execute_flow", _sub(0))]

    assert await worker._flow_pool_has_capacity() is True


@pytest.mark.asyncio
async def test_undelivered_flow_tasks_mean_no_free_slot() -> None:
    """Sixteen queued executions: the pool cannot absorb a seventeenth."""
    worker = _worker()
    worker.subs = [
        ("preloop.sync.tasks.execute_flow", _sub(16)),
        ("preloop.sync.tasks.resume_flow_execution", _sub(0)),
    ]

    assert await worker._flow_pool_has_capacity() is False


@pytest.mark.asyncio
async def test_the_wildcard_pool_is_asked_too() -> None:
    worker = PreloopSyncNatsWorker("nats://example.invalid:4222", "default-pool")
    worker.subs = [("preloop.sync.tasks.*", _sub(3))]

    assert await worker._flow_pool_has_capacity() is False


@pytest.mark.asyncio
async def test_an_unreachable_consumer_answers_unknown() -> None:
    """Unknown is not "no": the pass runs, as it did before this probe."""
    worker = _worker()
    worker.subs = [("preloop.sync.tasks.execute_flow", _sub(failing=True))]

    assert await worker._flow_pool_has_capacity() is None


@pytest.mark.asyncio
async def test_a_pool_without_flow_subjects_answers_unknown() -> None:
    worker = PreloopSyncNatsWorker(
        "nats://example.invalid:4222",
        "webhook-pool",
        tasks_allowlist=["process_webhook_event"],
    )
    worker.subs = [("preloop.sync.tasks.process_webhook_event", _sub(9))]

    assert await worker._flow_pool_has_capacity() is None

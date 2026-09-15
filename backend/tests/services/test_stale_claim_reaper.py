"""The stale-claim reaper must not shout into a queue nobody can drain.

The reaper re-publishes executions whose owning worker died. Its defect was
that "unclaimed" and "no capacity" look identical from the database, and
that every replica ran the same pass: three replicas re-published sixteen
queued executions every thirty seconds, about ninety publishes a minute,
until the rows were stopped by hand.

Three bounds are exercised here, with a fake clock so an hour of reaping
costs milliseconds: the lease (one pass at a time), the per-execution
backoff (growing gaps, persisted on the row), and the capacity skip. The
fourth test is the one that must never regress: killing the owner of a
running execution still gets it adopted inside one stale window.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services import execution_recovery
from preloop.services.execution_reaper import (
    ReaperPassSummary,
    is_redispatch_due,
    redispatch_backoff_seconds,
)
from preloop.services.execution_recovery import ExecutionRecoveryService


def _make_account(db: Session, name: str):
    account = crud_account.create(
        db,
        obj_in={"organization_name": name, "is_active": True, "meta_data": {}},
    )
    db.commit()
    db.refresh(account)
    return account


def _make_flow(db: Session, account_id):
    flow = crud_flow.create(
        db=db,
        flow_in=FlowCreate(
            name=f"reaper-test-{uuid.uuid4().hex[:8]}",
            prompt_template="hello",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="openhands",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            is_enabled=True,
            account_id=account_id,
        ),
        account_id=account_id,
    )
    db.commit()
    db.refresh(flow)
    return flow


def _pending(db: Session, flow_id):
    execution = crud_flow_execution.create(
        db,
        obj_in=FlowExecutionCreate(
            flow_id=flow_id,
            status="PENDING",
            trigger_event_details={"source": "test"},
        ),
    )
    db.commit()
    db.refresh(execution)
    return execution


def _orphaned_running(db: Session, flow_id, *, worker_id: str = "worker-dead"):
    """A RUNNING execution whose owner died: claimed, heartbeat long stale."""
    execution = _pending(db, flow_id)
    execution.status = "RUNNING"
    execution.agent_session_reference = f"job/{uuid.uuid4().hex[:8]}"
    execution.orchestrator_worker_id = worker_id
    execution.orchestrator_claimed_at = datetime.now(timezone.utc) - timedelta(hours=1)
    execution.orchestrator_heartbeat_at = datetime.now(timezone.utc) - timedelta(
        hours=1
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)
    return execution


class _FakeClock:
    """A clock the test moves by hand."""

    def __init__(self, start: Optional[datetime] = None):
        self.now = start or datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _recording_dispatch(published: List[str]):
    async def _dispatch(execution_id):
        published.append(str(execution_id))
        return True

    return _dispatch


class TestBackoffSchedule:
    """Growing gaps with a cap, not one publish per pass."""

    def test_the_first_publish_is_immediate(self):
        assert redispatch_backoff_seconds(0) == 0

    def test_the_gap_doubles_from_the_reclaim_interval(self):
        gaps = [
            redispatch_backoff_seconds(n, base_seconds=30, max_seconds=900)
            for n in range(1, 6)
        ]
        assert gaps == [30, 60, 120, 240, 480]

    def test_the_gap_stops_growing_at_the_cap(self):
        assert redispatch_backoff_seconds(20, base_seconds=30, max_seconds=900) == 900
        assert redispatch_backoff_seconds(400, base_seconds=30, max_seconds=900) == 900

    def test_a_row_that_was_never_published_is_due(self):
        assert is_redispatch_due(attempts=0, last_redispatch_at=None) is True

    def test_a_counter_without_a_timestamp_is_due(self):
        """An upgraded row must not be stuck: the safety net errs to running."""
        assert is_redispatch_due(attempts=5, last_redispatch_at=None) is True

    def test_a_naive_timestamp_is_read_as_utc(self):
        now = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
        just_published = datetime(2026, 9, 15, 11, 59, 45)  # naive, 15s ago
        assert (
            is_redispatch_due(
                attempts=1,
                last_redispatch_at=just_published,
                now=now,
                base_seconds=30,
            )
            is False
        )

    def test_an_hour_unclaimed_is_a_handful_of_publishes(self):
        """The acceptance number: bounded re-dispatches, not 120 in an hour."""
        clock = _FakeClock()
        started = clock.now
        attempts = 0
        last: Optional[datetime] = None
        publishes: List[datetime] = []

        # One pass every 30 seconds for an hour.
        while clock.now - started < timedelta(hours=1):
            if is_redispatch_due(
                attempts=attempts,
                last_redispatch_at=last,
                now=clock.now,
                base_seconds=30,
                max_seconds=900,
            ):
                attempts += 1
                last = clock.now
                publishes.append(clock.now)
            clock.advance(30)

        assert len(publishes) <= 8, publishes
        gaps = [
            (later - earlier).total_seconds()
            for earlier, later in zip(publishes, publishes[1:], strict=False)
        ]
        assert gaps == sorted(gaps), gaps
        assert gaps[0] < gaps[-1]


class TestReaperLease:
    """One pass at a time, whatever the replica count."""

    def test_a_second_connection_does_not_get_the_lease(self, db_engine):
        """Two workers, one lease: the loser skips its pass."""
        first = Session(bind=db_engine.connect())
        second = Session(bind=db_engine.connect())
        try:
            with crud_flow_execution.stale_claim_reaper_lease(
                first, holder="worker-a"
            ) as leased_first:
                assert leased_first is True
                with crud_flow_execution.stale_claim_reaper_lease(
                    second, holder="worker-b"
                ) as leased_second:
                    assert leased_second is False
        finally:
            first.close()
            second.close()

    def test_the_lease_is_released_for_the_next_pass(self, db_engine):
        """Losing is not an error and the winner does not keep the lease."""
        first = Session(bind=db_engine.connect())
        second = Session(bind=db_engine.connect())
        try:
            with crud_flow_execution.stale_claim_reaper_lease(first) as leased:
                assert leased is True
            with crud_flow_execution.stale_claim_reaper_lease(second) as leased:
                assert leased is True
        finally:
            first.close()
            second.close()

    def test_a_non_postgres_session_always_wins(self):
        """Single-process dev has no second reaper to exclude."""
        db = MagicMock()
        db.bind.dialect.name = "sqlite"
        with crud_flow_execution.stale_claim_reaper_lease(db) as leased:
            assert leased is True
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_only_the_lease_holder_re_dispatches(
        self, db_session: Session, db_engine
    ):
        """With N replicas and one stale execution, one re-dispatch, not N."""
        account = _make_account(db_session, "Lease Tenant")
        flow = _make_flow(db_session, account.id)
        _pending(db_session, flow.id)

        published: List[str] = []
        dispatch = _recording_dispatch(published)
        from preloop.services import flow_execution_dispatcher

        holder = db_engine.connect()
        try:
            # Another replica is already mid-pass.
            holder.execute(
                text(
                    "SELECT pg_try_advisory_lock("
                    "hashtextextended('flow_execution_stale_claim_reaper', 0))"
                )
            )
            holder.commit()

            with (
                patch.object(flow_execution_dispatcher, "dispatch_execute", dispatch),
                patch.object(flow_execution_dispatcher, "dispatch_resume", dispatch),
                patch.object(
                    flow_execution_dispatcher,
                    "flow_execution_worker_enabled",
                    return_value=True,
                ),
            ):
                skipped = await execution_recovery.get_recovery_service().recover_orphaned_executions(
                    db_session
                )
                assert skipped == 0
                assert published == []

                # The holder finishes its pass and releases.
                holder.execute(
                    text(
                        "SELECT pg_advisory_unlock("
                        "hashtextextended('flow_execution_stale_claim_reaper', 0))"
                    )
                )
                holder.commit()

                dispatched = (
                    await ExecutionRecoveryService().recover_orphaned_executions(
                        db_session
                    )
                )

            assert dispatched == 1
            assert len(published) == 1
        finally:
            holder.close()


class TestCapacitySkip:
    """A pass that cannot be drained publishes nothing and says so."""

    @pytest.mark.asyncio
    async def test_no_free_slot_means_no_publish(self, db_session: Session, caplog):
        account = _make_account(db_session, "No Capacity Tenant")
        flow = _make_flow(db_session, account.id)
        for _ in range(3):
            _pending(db_session, flow.id)

        service = ExecutionRecoveryService()
        published: List[str] = []
        dispatch = _recording_dispatch(published)
        from preloop.services import flow_execution_dispatcher

        async def no_capacity() -> bool:
            return False

        with (
            caplog.at_level(logging.INFO, logger="preloop.services.execution_recovery"),
            patch.object(flow_execution_dispatcher, "dispatch_execute", dispatch),
            patch.object(flow_execution_dispatcher, "dispatch_resume", dispatch),
        ):
            dispatched = await service._redispatch_stale_executions(
                db_session,
                stale_after_seconds=120,
                capacity_probe=no_capacity,
            )

        assert dispatched == 0
        assert published == []
        summary = [
            record.getMessage()
            for record in caplog.records
            if "Stale-claim reaper pass" in record.getMessage()
        ]
        assert len(summary) == 1
        assert "skipped_no_capacity=3" in summary[0]
        assert "re-dispatched=0" in summary[0]

    @pytest.mark.asyncio
    async def test_a_live_container_is_adopted_even_with_no_free_slot(
        self, db_session: Session
    ):
        """An unmonitored container is worse than being one over the limit."""
        account = _make_account(db_session, "Adopt Tenant")
        flow = _make_flow(db_session, account.id)
        orphan = _orphaned_running(db_session, flow.id)
        _pending(db_session, flow.id)

        service = ExecutionRecoveryService()
        published: List[str] = []
        dispatch = _recording_dispatch(published)
        from preloop.services import flow_execution_dispatcher

        async def no_capacity() -> bool:
            return False

        with (
            patch.object(flow_execution_dispatcher, "dispatch_execute", dispatch),
            patch.object(flow_execution_dispatcher, "dispatch_resume", dispatch),
        ):
            dispatched = await service._redispatch_stale_executions(
                db_session,
                stale_after_seconds=120,
                capacity_probe=no_capacity,
            )

        assert dispatched == 1
        assert published == [str(orphan.id)]

    @pytest.mark.asyncio
    async def test_a_failing_probe_does_not_stop_recovery(self, db_session: Session):
        """An unknown answer must not disable the deploy-handoff safety net."""
        account = _make_account(db_session, "Probe Failure Tenant")
        flow = _make_flow(db_session, account.id)
        execution = _pending(db_session, flow.id)

        service = ExecutionRecoveryService()
        published: List[str] = []
        dispatch = _recording_dispatch(published)
        from preloop.services import flow_execution_dispatcher

        async def broken() -> bool:
            raise RuntimeError("no connection")

        with (
            patch.object(flow_execution_dispatcher, "dispatch_execute", dispatch),
            patch.object(flow_execution_dispatcher, "dispatch_resume", dispatch),
        ):
            dispatched = await service._redispatch_stale_executions(
                db_session,
                stale_after_seconds=120,
                capacity_probe=broken,
            )

        assert dispatched == 1
        assert published == [str(execution.id)]


class TestPersistedBackoff:
    """The backoff is on the row, so every replica reads the same schedule."""

    @pytest.mark.asyncio
    async def test_a_pass_records_the_publish_on_the_row(self, db_session: Session):
        account = _make_account(db_session, "Recorded Tenant")
        flow = _make_flow(db_session, account.id)
        execution = _pending(db_session, flow.id)
        assert execution.redispatch_count == 0

        clock = _FakeClock()
        service = ExecutionRecoveryService()
        published: List[str] = []
        dispatch = _recording_dispatch(published)
        from preloop.services import flow_execution_dispatcher

        with (
            patch.object(execution_recovery, "_utcnow", clock),
            patch.object(flow_execution_dispatcher, "dispatch_execute", dispatch),
            patch.object(flow_execution_dispatcher, "dispatch_resume", dispatch),
        ):
            await service._redispatch_stale_executions(
                db_session, stale_after_seconds=120
            )

        db_session.refresh(execution)
        assert execution.redispatch_count == 1
        assert execution.last_redispatch_at is not None

    @pytest.mark.asyncio
    async def test_a_fresh_service_still_honours_the_backoff(self, db_session: Session):
        """A restarted (or different) worker reads the same counter."""
        account = _make_account(db_session, "Shared Backoff Tenant")
        flow = _make_flow(db_session, account.id)
        _pending(db_session, flow.id)

        clock = _FakeClock()
        published: List[str] = []
        dispatch = _recording_dispatch(published)
        from preloop.services import flow_execution_dispatcher

        with (
            patch.object(execution_recovery, "_utcnow", clock),
            patch.object(flow_execution_dispatcher, "dispatch_execute", dispatch),
            patch.object(flow_execution_dispatcher, "dispatch_resume", dispatch),
        ):
            await ExecutionRecoveryService()._redispatch_stale_executions(
                db_session, stale_after_seconds=120
            )
            clock.advance(1)
            await ExecutionRecoveryService()._redispatch_stale_executions(
                db_session, stale_after_seconds=120
            )

        assert len(published) == 1

    @pytest.mark.asyncio
    async def test_the_gaps_grow_across_passes(self, db_session: Session):
        """Publish, wait 30s, publish, wait 30s, nothing; wait 60s, publish."""
        account = _make_account(db_session, "Growing Gap Tenant")
        flow = _make_flow(db_session, account.id)
        _pending(db_session, flow.id)

        clock = _FakeClock()
        service = ExecutionRecoveryService()
        published: List[str] = []
        dispatch = _recording_dispatch(published)
        from preloop.services import flow_execution_dispatcher

        with (
            patch.object(execution_recovery, "_utcnow", clock),
            patch.object(flow_execution_dispatcher, "dispatch_execute", dispatch),
            patch.object(flow_execution_dispatcher, "dispatch_resume", dispatch),
        ):
            await service._redispatch_stale_executions(
                db_session, stale_after_seconds=120
            )
            assert len(published) == 1

            clock.advance(30)
            await service._redispatch_stale_executions(
                db_session, stale_after_seconds=120
            )
            assert len(published) == 2

            clock.advance(30)
            await service._redispatch_stale_executions(
                db_session, stale_after_seconds=120
            )
            assert len(published) == 2  # the second gap is 60 seconds

            clock.advance(30)
            await service._redispatch_stale_executions(
                db_session, stale_after_seconds=120
            )
            assert len(published) == 3

    @pytest.mark.asyncio
    async def test_an_hour_of_passes_is_bounded(self, db_session: Session):
        """120 passes in an hour, a single-digit number of publishes."""
        account = _make_account(db_session, "Hour Tenant")
        flow = _make_flow(db_session, account.id)
        _pending(db_session, flow.id)

        clock = _FakeClock()
        service = ExecutionRecoveryService()
        published: List[str] = []
        dispatch = _recording_dispatch(published)
        from preloop.services import flow_execution_dispatcher

        with (
            patch.object(execution_recovery, "_utcnow", clock),
            patch.object(flow_execution_dispatcher, "dispatch_execute", dispatch),
            patch.object(flow_execution_dispatcher, "dispatch_resume", dispatch),
        ):
            for _ in range(120):
                await service._redispatch_stale_executions(
                    db_session, stale_after_seconds=120
                )
                clock.advance(30)

        assert 1 <= len(published) <= 8, len(published)


class TestOwnerDeathStillRecovers:
    """The safety net this loop exists for must keep working."""

    @pytest.mark.asyncio
    async def test_a_dead_owner_is_re_dispatched_within_one_window(
        self, db_session: Session
    ):
        account = _make_account(db_session, "Dead Owner Tenant")
        flow = _make_flow(db_session, account.id)
        orphan = _orphaned_running(db_session, flow.id)

        service = ExecutionRecoveryService()
        published: List[str] = []
        dispatch = _recording_dispatch(published)
        from preloop.services import flow_execution_dispatcher

        with (
            patch.object(flow_execution_dispatcher, "dispatch_execute", dispatch),
            patch.object(flow_execution_dispatcher, "dispatch_resume", dispatch),
        ):
            dispatched = await service._redispatch_stale_executions(
                db_session, stale_after_seconds=120
            )

        assert dispatched == 1
        assert published == [str(orphan.id)]

    @pytest.mark.asyncio
    async def test_a_claim_clears_the_backoff(self, db_session: Session):
        """A run that queued for an hour is adopted at once when it dies."""
        account = _make_account(db_session, "Requeued Tenant")
        flow = _make_flow(db_session, account.id)
        execution = _pending(db_session, flow.id)
        crud_flow_execution.record_redispatch(db_session, execution_ids=[execution.id])
        db_session.refresh(execution)
        assert execution.redispatch_count == 1

        claimed = crud_flow_execution.claim_execution(
            db_session,
            execution_id=execution.id,
            worker_id="worker-new",
            stale_after_seconds=120,
        )
        assert claimed is not None
        assert claimed.redispatch_count == 0
        assert claimed.last_redispatch_at is None


class TestPassSummary:
    """One line per pass, with the counts in it."""

    def test_the_summary_names_every_count(self):
        summary = ReaperPassSummary(
            candidates=16,
            redispatched=1,
            skipped_backoff=10,
            skipped_account_cap=4,
            skipped_no_capacity=1,
        )
        line = summary.as_log_line()
        assert "candidates=16" in line
        assert "re-dispatched=1" in line
        assert "skipped_backoff=10" in line
        assert "skipped_account_cap=4" in line
        assert "skipped_no_capacity=1" in line
        assert "failed=0" in line

    @pytest.mark.asyncio
    async def test_a_pass_logs_exactly_one_summary_line(
        self, db_session: Session, caplog
    ):
        account = _make_account(db_session, "Summary Tenant")
        flow = _make_flow(db_session, account.id)
        for _ in range(4):
            _pending(db_session, flow.id)

        service = ExecutionRecoveryService()
        published: List[str] = []
        dispatch = _recording_dispatch(published)
        from preloop.services import flow_execution_dispatcher

        with (
            caplog.at_level(logging.INFO, logger="preloop.services.execution_recovery"),
            patch.object(flow_execution_dispatcher, "dispatch_execute", dispatch),
            patch.object(flow_execution_dispatcher, "dispatch_resume", dispatch),
            patch(
                "preloop.services.execution_concurrency.account_running_cap",
                return_value=1,
            ),
        ):
            await service._redispatch_stale_executions(
                db_session, stale_after_seconds=120
            )

        lines = [
            record.getMessage()
            for record in caplog.records
            if "Stale-claim reaper pass" in record.getMessage()
        ]
        assert len(lines) == 1
        assert "candidates=4" in lines[0]
        assert "re-dispatched=1" in lines[0]
        assert "skipped_account_cap=3" in lines[0]

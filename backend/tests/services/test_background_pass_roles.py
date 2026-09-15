"""Background passes must stay out of a process that serves gateway traffic."""

import pytest

from preloop.services.audit_chain import AuditChainSealer
from preloop.services.retention_purge import RetentionPurgeSweeper
from preloop.services.service_roles import (
    background_passes_allowed,
    is_dedicated_gateway,
    serves_gateway_traffic,
)
from preloop.services.session_optimization_jobs import OptimizationJobSweeper


@pytest.mark.parametrize(
    ("role", "gateway_traffic", "background_passes"),
    [
        ("gateway", True, False),
        ("api", False, True),
        ("all", True, True),
        ("", True, True),
    ],
)
def test_role_decides_who_runs_background_passes(
    monkeypatch, role, gateway_traffic, background_passes
):
    """A dedicated gateway serves model traffic and runs no periodic passes."""
    monkeypatch.setenv("PRELOOP_SERVICE_ROLE", role)

    assert serves_gateway_traffic() is gateway_traffic
    assert background_passes_allowed() is background_passes
    assert is_dedicated_gateway() is (role == "gateway")


@pytest.mark.parametrize(
    "sweeper_factory",
    [
        lambda: AuditChainSealer(check_interval_seconds=3600),
        lambda: RetentionPurgeSweeper(check_interval_seconds=3600),
        lambda: OptimizationJobSweeper(check_interval_seconds=3600),
    ],
    ids=["audit_chain_sealer", "retention_purge", "optimization_jobs"],
)
@pytest.mark.asyncio
async def test_sweepers_refuse_to_start_on_a_gateway_pod(monkeypatch, sweeper_factory):
    """Starting a sweeper on the gateway role is a no-op, not a background task."""
    monkeypatch.setenv("PRELOOP_SERVICE_ROLE", "gateway")
    sweeper = sweeper_factory()

    await sweeper.start()

    assert sweeper._running is False
    assert sweeper._task is None


@pytest.mark.parametrize(
    ("sweeper_factory", "pass_owner", "pass_name"),
    [
        (
            lambda: AuditChainSealer(check_interval_seconds=3600),
            AuditChainSealer,
            "_seal_once",
        ),
        (
            lambda: RetentionPurgeSweeper(check_interval_seconds=3600),
            RetentionPurgeSweeper,
            "_sweep_once",
        ),
        (
            lambda: OptimizationJobSweeper(check_interval_seconds=3600),
            OptimizationJobSweeper,
            "_sweep_once",
        ),
    ],
    ids=["audit_chain_sealer", "retention_purge", "optimization_jobs"],
)
@pytest.mark.asyncio
async def test_sweepers_still_start_on_the_api_role(
    monkeypatch, sweeper_factory, pass_owner, pass_name
):
    """Single-process and API deployments keep their periodic passes."""
    monkeypatch.setenv("PRELOOP_SERVICE_ROLE", "api")
    # The pass itself is not under test here; only who is allowed to run it.
    monkeypatch.setattr(pass_owner, pass_name, staticmethod(lambda: None))
    sweeper = sweeper_factory()

    await sweeper.start()
    try:
        assert sweeper._running is True
        assert sweeper._task is not None
    finally:
        await sweeper.stop()

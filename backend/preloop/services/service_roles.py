"""Which work a process is allowed to do, derived from its service role.

A deployment splits the same image into roles with ``PRELOOP_SERVICE_ROLE``:
``api`` serves the console and API, ``gateway`` serves model traffic, and
``all`` is the single-process default used by compose and single-node
installs.

Background passes (sealers, sweepers, purges) walk every account and hold
their working set for the length of a pass. A pod that relays model
responses already holds request and response bodies for every in-flight
call, so adding a sweeper to it means two unrelated working sets competing
for the same memory limit, and the failure mode is an OOMKill that takes
the in-flight model calls of every account on that replica with it.

Hence the rule this module exists to make explicit and testable: a
dedicated gateway process runs no background passes. Roles ``api`` and
``all`` still run them, so nothing changes for single-process installs.
"""

from __future__ import annotations

import os

API_ROLE = "api"
GATEWAY_ROLE = "gateway"
ALL_ROLES = "all"


def current_service_role() -> str:
    """Return the normalized role of this process."""
    return os.getenv("PRELOOP_SERVICE_ROLE", ALL_ROLES).strip().lower() or ALL_ROLES


def serves_api_traffic(role: str | None = None) -> bool:
    """Whether this process serves console and API requests."""
    return (role or current_service_role()) in {ALL_ROLES, API_ROLE}


def serves_gateway_traffic(role: str | None = None) -> bool:
    """Whether this process relays model gateway requests."""
    return (role or current_service_role()) in {ALL_ROLES, GATEWAY_ROLE}


def is_dedicated_gateway(role: str | None = None) -> bool:
    """Whether this process serves gateway traffic and nothing else."""
    return (role or current_service_role()) == GATEWAY_ROLE


def background_passes_allowed(role: str | None = None) -> bool:
    """Whether periodic account-wide passes may run in this process."""
    resolved = role or current_service_role()
    return serves_api_traffic(resolved) and not is_dedicated_gateway(resolved)

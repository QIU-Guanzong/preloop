"""Gateway create_app must not pay the control-plane import tax.

These probes run in a child interpreter so pytest's session-wide
``create_app`` import cannot contaminate ``sys.modules``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND_ROOT.parent

_API_ONLY_MODULES = (
    "preloop.services.flow_orchestrator",
    "preloop.services.mcp_http",
    "preloop.services.mcp_client_pool",
    "preloop.api.auth.router",
    "preloop.api.endpoints.flows",
    "preloop.api.endpoints.issues",
    "preloop.api.endpoints.websockets",
)

_GATEWAY_MODULES = (
    "preloop.api.endpoints.openai_gateway",
    "preloop.api.endpoints.anthropic_gateway",
    "preloop.api.endpoints.gemini_gateway",
    "preloop.api.endpoints.health",
    "preloop.api.endpoints.version",
)

_PROBE = r"""
import json
import os
import resource
import sys
import tracemalloc

role = sys.argv[1]
os.environ["PRELOOP_SERVICE_ROLE"] = role
os.environ.setdefault("TESTING", "true")
os.environ.setdefault("DISABLE_RBAC", "true")
os.environ.setdefault("PRELOOP_DISABLE_TELEMETRY", "true")

tracemalloc.start()
from preloop.api.app import create_app

app = create_app()
current, peak = tracemalloc.get_traced_memory()
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
from tests.route_paths import collect_route_paths
paths = sorted(collect_route_paths(app.routes))
print(
    json.dumps(
        {
            "role": role,
            "modules": sorted(sys.modules),
            "module_count": len(sys.modules),
            "rss": rss,
            "tracemalloc_current": current,
            "tracemalloc_peak": peak,
            "litellm_local_cost_map": os.environ.get(
                "LITELLM_LOCAL_MODEL_COST_MAP"
            ),
            "paths": paths,
        }
    )
)
"""


def _measure_create_app(role: str) -> dict[str, Any]:
    """Spawn a clean interpreter and create the app for ``role``."""
    env = os.environ.copy()
    env["PRELOOP_SERVICE_ROLE"] = role
    env["TESTING"] = "true"
    env["DISABLE_RBAC"] = "true"
    env["PRELOOP_DISABLE_TELEMETRY"] = "true"
    pythonpath = env.get("PYTHONPATH", "")
    backend = str(_BACKEND_ROOT)
    env["PYTHONPATH"] = (
        backend if not pythonpath else os.pathsep.join([backend, pythonpath])
    )
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE, role],
        cwd=_REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        return json.loads(completed.stdout.splitlines()[-1])
    except json.JSONDecodeError as exc:  # pragma: no cover - probe failure
        raise AssertionError(
            f"create_app probe for role={role!r} did not return JSON\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        ) from exc


@pytest.fixture(scope="module")
def gateway_probe() -> dict[str, Any]:
    return _measure_create_app("gateway")


@pytest.fixture(scope="module")
def api_probe() -> dict[str, Any]:
    return _measure_create_app("api")


def test_gateway_create_app_skips_api_only_modules(
    gateway_probe: dict[str, Any],
) -> None:
    modules = set(gateway_probe["modules"])
    missing = [name for name in _API_ONLY_MODULES if name in modules]
    assert missing == [], (
        f"gateway create_app imported control-plane modules: {missing}"
    )
    for name in _GATEWAY_MODULES:
        assert name in modules, f"gateway create_app missed {name}"
    assert "/api/v1/health" in gateway_probe["paths"]
    assert "/openai/v1/models" in gateway_probe["paths"]
    assert "/api/v1/trackers" not in gateway_probe["paths"]
    assert gateway_probe["litellm_local_cost_map"] == "true"


def test_api_create_app_loads_control_plane_not_gateway(
    api_probe: dict[str, Any],
) -> None:
    modules = set(api_probe["modules"])
    assert "preloop.services.mcp_http" in modules
    assert "preloop.api.endpoints.flows" in modules
    assert "preloop.api.endpoints.openai_gateway" not in modules
    assert "/api/v1/trackers" in api_probe["paths"]
    assert "/openai/v1/models" not in api_probe["paths"]


def test_gateway_create_app_imports_fewer_modules_than_api(
    gateway_probe: dict[str, Any], api_probe: dict[str, Any]
) -> None:
    assert gateway_probe["module_count"] < api_probe["module_count"]
    # ru_maxrss is bytes on macOS and kilobytes on Linux. Compare like-for-like
    # within one host; skip if the platform reports identical peaks.
    if gateway_probe["rss"] and api_probe["rss"]:
        assert gateway_probe["rss"] <= api_probe["rss"]

"""Helpers for the GitHub CI workflow drift guards.

Several test modules read ``.github/workflows/ci.yml`` and assert on its
shape. They share one parse and one path so a rename or a restructure fails
in one place instead of drifting between copies.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"


def load_ci_jobs() -> dict[str, Any]:
    """Return the jobs mapping from the GitHub CI workflow."""
    with CI_WORKFLOW.open(encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    jobs = doc["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def step_script(job: dict[str, Any], name: str) -> str:
    """Return the run script for a named workflow step."""
    for step in job["steps"]:
        if step.get("name") == name:
            script = step["run"]
            assert isinstance(script, str)
            return script
    raise AssertionError(f"No step named {name!r}")

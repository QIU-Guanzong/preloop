"""Guard the self-hosted runner routing in the GitHub CI workflow.

The test suites run on a private Debian VM when it is online and idle, and on
GitHub's public runners the rest of the time. That decision lives in the
``pick-runner`` job; every other job stays pinned. Two properties matter enough
to pin down here:

1. Routing reaches exactly the test jobs. A code-quality job that drifted onto
   the single self-hosted VM would serialize behind the test suites, and the
   Windows CLI job cannot run on a Linux runner at all.
2. The fallback is unconditional. ``pick-runner`` must never fail the workflow
   and must never leave ``test_runner`` unset: a missing secret, a token
   without ``administration: read``, or an API error all have to land on
   ``ubuntu-latest``. Otherwise an unrelated PR goes red over CI plumbing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

PICKED_RUNS_ON = "${{ fromJSON(needs.pick-runner.outputs.test_runner) }}"
ROUTED_JOBS = (
    "test-backend",
    "test-backend-coverage",
    "test-frontend",
    "test-runtime-plugins",
)
PINNED_JOBS = (
    "changes",
    "pick-runner",
    "requirements-resolve",
    "lint",
    "helm-lint",
    "cli-vuln-scan",
    "build-and-push",
    "ci",
)


def _load_ci_jobs() -> dict[str, Any]:
    """Return the jobs mapping from the GitHub CI workflow."""
    with CI_WORKFLOW.open(encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    jobs = doc["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _pick_script() -> str:
    """Return the run script of the pick-runner decision step."""
    for step in _load_ci_jobs()["pick-runner"]["steps"]:
        if step.get("id") == "pick":
            script = step["run"]
            assert isinstance(script, str)
            return script
    raise AssertionError("pick-runner has no step with id 'pick'")


def test_test_jobs_route_through_pick_runner() -> None:
    """Every test suite asks pick-runner where to run, and waits for it."""
    jobs = _load_ci_jobs()
    for name in ROUTED_JOBS:
        job = jobs[name]
        assert job["runs-on"] == PICKED_RUNS_ON, name
        assert "pick-runner" in job["needs"], name


def test_non_test_jobs_stay_on_their_own_runners() -> None:
    """Code-quality jobs and the Windows CLI job are not rerouted."""
    jobs = _load_ci_jobs()
    for name in PINNED_JOBS:
        assert jobs[name]["runs-on"] == "ubuntu-latest", name
    assert jobs["test-cli-windows"]["runs-on"] == "windows-latest"


def test_pick_runner_decides_on_a_public_runner() -> None:
    """The chooser cannot depend on the thing it is choosing."""
    pick = _load_ci_jobs()["pick-runner"]
    assert pick["runs-on"] == "ubuntu-latest"
    assert pick["outputs"]["test_runner"] == "${{ steps.pick.outputs.test_runner }}"
    assert "timeout-minutes" in pick


def test_pick_runner_falls_back_to_the_public_runner() -> None:
    """Missing secret, disabled variable, or API error all mean ubuntu-latest."""
    script = _pick_script()
    assert "PUBLIC='\"ubuntu-latest\"'" in script
    # The knob that turns routing off without a commit.
    assert "CI_SELF_HOSTED_TESTS" in str(_load_ci_jobs()["pick-runner"])
    assert '"${SELF_HOSTED_TESTS:-true}" = "false"' in script
    # An absent secret is the state on forks and Dependabot PRs.
    assert '-z "${GH_TOKEN:-}"' in script
    # A failed API call must not abort the step.
    assert "could not list repository runners" in script
    assert "could not parse the runner list" in script
    # `set -e` would turn any of the above into a red required check.
    assert "set -e" not in script


def test_pick_runner_requires_an_idle_matching_runner() -> None:
    """Only an online, not-busy runner carrying all three labels counts."""
    script = _pick_script()
    assert 'SELF_HOSTED=\'["self-hosted","Linux","X64"]\'' in script
    assert '.status == "online"' in script
    assert ".busy == false" in script
    for label in ("self-hosted", "Linux", "X64"):
        assert f'index("{label}")' in script


def test_ci_aggregator_fails_when_pick_runner_fails() -> None:
    """A crashed pick must not read as a path-filter skip on the required check."""
    aggregator = _load_ci_jobs()["ci"]
    assert "pick-runner" in aggregator["needs"]
    for step in aggregator["steps"]:
        if step.get("name") == "Require every suite to have passed or been skipped":
            assert "check pick-runner" in step["run"]
            assert step["env"]["PICKRUNNER"] == "${{ needs.pick-runner.result }}"
            return
    raise AssertionError("ci aggregator has no result-check step")


def test_root_only_steps_are_gated_to_the_github_image() -> None:
    """sudo/apt steps must not run on a VM whose user may have no sudo."""
    jobs = _load_ci_jobs()
    for name in ROUTED_JOBS:
        for step in jobs[name]["steps"]:
            script = step.get("run", "")
            if not isinstance(script, str):
                continue
            if "sudo" in script or "apt-get" in script:
                assert step.get("if") == "runner.environment == 'github-hosted'", (
                    f"{name}: step {step.get('name')!r} needs root but is not gated"
                )


def test_routed_jobs_are_bounded_and_start_clean() -> None:
    """A reused self-hosted workspace gets wiped, and no job can hang forever."""
    jobs = _load_ci_jobs()
    for name in ROUTED_JOBS:
        job = jobs[name]
        assert isinstance(job["timeout-minutes"], int), name
        checkout = next(
            step
            for step in job["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        assert checkout["with"]["clean"] is True, name

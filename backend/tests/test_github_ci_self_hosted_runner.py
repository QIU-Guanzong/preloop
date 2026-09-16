"""Guard the self-hosted runner routing in the GitHub CI workflow.

ubuntu-latest is the default pool. Idle private VMs take overflow backend
shards only; GitHub will wait forever on a self-hosted ``runs-on`` rather
than fall back to hosted, so sending every test job to three VMs serializes
the suite. Two properties matter enough to pin down here:

1. Routing reaches only backend shards, per group. Frontend, plugins, and
   coverage stay on ubuntu-latest and must not wait for pick-runner.
2. The fallback is unconditional. ``pick-runner`` must never fail the
   workflow and must never leave ``backend_plan`` unset: a missing secret,
   a token without ``administration: read``, or an API error all have to
   land on eight hosted shards. Otherwise an unrelated PR goes red over
   CI plumbing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from tests.ci_workflow import load_ci_jobs
from tests.test_github_ci_backend_shards import BACKEND_TEST_SPLITS

BACKEND_SHARD = "[matrix.group - 1]"
HOSTED_JOBS = (
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
    *HOSTED_JOBS,
)
TEST_JOBS = ("test-backend", *HOSTED_JOBS)


def _pick_script() -> str:
    """Return the run script of the pick-runner decision step."""
    for step in load_ci_jobs()["pick-runner"]["steps"]:
        if step.get("id") == "pick":
            script = step["run"]
            assert isinstance(script, str)
            return script
    raise AssertionError("pick-runner has no step with id 'pick'")


def _plan_from_jq(idle: int) -> list[dict[str, Any]]:
    """Run the workflow's jq program against an idle-runner count."""
    jq = shutil.which("jq")
    if jq is None:
        raise AssertionError("jq is required to check backend_plan")
    script = _pick_script()
    start = script.index("jq -cn '") + len("jq -cn '")
    end = script.index("') || plan=\"\"", start)
    program = script[start:end]
    completed = subprocess.run(
        [jq, "-cn", program],
        check=True,
        capture_output=True,
        text=True,
        env={"IDLE": str(idle), "PYVER": "3.11"},
    )
    plan = json.loads(completed.stdout)
    assert isinstance(plan, list)
    return plan


def test_backend_shards_route_through_pick_runner_plan() -> None:
    """Each backend shard reads its own slot from backend_plan."""
    backend = load_ci_jobs()["test-backend"]
    assert BACKEND_SHARD in backend["runs-on"]
    assert "backend_plan" in backend["runs-on"]
    assert "pick-runner" in backend["needs"]
    assert backend["container"] == (
        "${{ fromJSON(needs.pick-runner.outputs.backend_plan)"
        f"{BACKEND_SHARD}.container }}}}"
    )


def test_extra_suites_stay_on_hosted_and_do_not_wait() -> None:
    """Frontend, plugins, and coverage must not serialize behind private VMs."""
    jobs = load_ci_jobs()
    for name in HOSTED_JOBS:
        job = jobs[name]
        assert job["runs-on"] == "ubuntu-latest", name
        assert "pick-runner" not in job["needs"], name


def test_non_test_jobs_stay_on_their_own_runners() -> None:
    """Code-quality jobs and the Windows CLI job are not rerouted."""
    jobs = load_ci_jobs()
    for name in PINNED_JOBS:
        assert jobs[name]["runs-on"] == "ubuntu-latest", name
    assert jobs["test-cli-windows"]["runs-on"] == "windows-latest"


def test_pick_runner_decides_on_a_public_runner() -> None:
    """The chooser cannot depend on the thing it is choosing."""
    pick = load_ci_jobs()["pick-runner"]
    assert pick["runs-on"] == "ubuntu-latest"
    assert pick["outputs"]["backend_plan"] == (
        "${{ steps.pick.outputs.backend_plan }}"
    )
    assert "timeout-minutes" in pick
    assert "test_runner" not in pick["outputs"]


def test_pick_runner_falls_back_to_the_public_runner() -> None:
    """Missing secret, disabled variable, or API error all mean ubuntu-latest."""
    script = _pick_script()
    # The knob that turns overflow off without a commit.
    assert "CI_SELF_HOSTED_TESTS" in str(load_ci_jobs()["pick-runner"])
    assert '"${SELF_HOSTED_TESTS:-true}" = "false"' in script
    # An absent secret is the state on forks and Dependabot PRs.
    assert '-z "${GH_TOKEN:-}"' in script
    # A failed API call must not abort the step.
    assert "could not list repository runners" in script
    assert "could not parse the runner list" in script
    # `set -e` would turn any of the above into a red required check.
    assert "set -e" not in script
    # Fallback must emit hosted Postgres, not a job container.
    assert 'db_host:"localhost"' in script
    assert "container:null" in script
    assert 'db_host:"postgres"' in script
    assert "postgres_ports:[]" in script
    # All-or-nothing self-hosted routing is what made CI slower.
    assert 'pick "$SELF_HOSTED"' not in script
    assert "emit 0 " in script


def test_pick_runner_requires_an_idle_matching_runner() -> None:
    """Only an online, not-busy runner carrying all three labels counts."""
    script = _pick_script()
    assert '["self-hosted","Linux","X64"]' in script
    assert '.status == "online"' in script
    assert ".busy == false" in script
    for label in ("self-hosted", "Linux", "X64"):
        assert f'index("{label}")' in script
    # Hosted first; idle VMs take the tail of the matrix.
    assert "range(0;8)" in script
    assert ". >= (8 - $idle)" in script


def test_three_idle_runners_only_overflow_the_last_three_shards() -> None:
    """Three VMs take shards 6-8; groups 1-5 (including the long pole) stay hosted."""
    plan = _plan_from_jq(3)
    assert len(plan) == BACKEND_TEST_SPLITS
    hosted = plan[:5]
    overflow = plan[5:]
    assert all(slot["runner"] == "ubuntu-latest" for slot in hosted)
    assert all(slot["container"] is None for slot in hosted)
    assert all(slot["db_host"] == "localhost" for slot in hosted)
    assert all(slot["postgres_ports"] == ["5432:5432"] for slot in hosted)
    assert all(slot["runner"] == ["self-hosted", "Linux", "X64"] for slot in overflow)
    assert all(
        slot["container"] == {"image": "python:3.11-bookworm"} for slot in overflow
    )
    assert all(slot["db_host"] == "postgres" for slot in overflow)
    assert all(slot["postgres_ports"] == [] for slot in overflow)


def test_zero_idle_runners_keeps_every_shard_on_hosted() -> None:
    """No private capacity means the matrix matches pre-self-hosted CI."""
    plan = _plan_from_jq(0)
    assert len(plan) == BACKEND_TEST_SPLITS
    assert all(slot["runner"] == "ubuntu-latest" for slot in plan)
    assert all(slot["container"] is None for slot in plan)


def test_ci_aggregator_fails_when_pick_runner_fails() -> None:
    """A crashed pick must not read as a path-filter skip on the required check."""
    aggregator = load_ci_jobs()["ci"]
    assert "pick-runner" in aggregator["needs"]
    for step in aggregator["steps"]:
        if step.get("name") == "Require every suite to have passed or been skipped":
            assert "check pick-runner" in step["run"]
            assert step["env"]["PICKRUNNER"] == "${{ needs.pick-runner.result }}"
            return
    raise AssertionError("ci aggregator has no result-check step")


def test_root_only_steps_are_gated_to_the_github_image() -> None:
    """sudo/apt steps must not run on a VM whose user may have no sudo."""
    jobs = load_ci_jobs()
    for name in TEST_JOBS:
        for step in jobs[name]["steps"]:
            script = step.get("run", "")
            if not isinstance(script, str):
                continue
            if "sudo" in script or "apt-get" in script:
                assert step.get("if") == "runner.environment == 'github-hosted'", (
                    f"{name}: step {step.get('name')!r} needs root but is not gated"
                )


def test_test_jobs_are_bounded_and_start_clean() -> None:
    """A reused self-hosted workspace gets wiped, and no job can hang forever."""
    jobs = load_ci_jobs()
    for name in TEST_JOBS:
        job = jobs[name]
        assert isinstance(job["timeout-minutes"], int), name
        checkout = next(
            step
            for step in job["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        assert checkout["with"]["clean"] is True, name


def test_backend_postgres_network_follows_the_shard_plan() -> None:
    """Hosted shards keep localhost:5432; overflow shards do not bind the host port."""
    pick = load_ci_jobs()["pick-runner"]
    assert pick["outputs"]["backend_plan"] == (
        "${{ steps.pick.outputs.backend_plan }}"
    )
    script = _pick_script()
    assert "python:" in script and "-bookworm" in script
    assert 'postgres_ports:["5432:5432"]' in script

    backend = load_ci_jobs()["test-backend"]
    assert BACKEND_SHARD in backend["container"]
    assert backend["services"]["postgres"]["ports"] == (
        "${{ fromJSON(needs.pick-runner.outputs.backend_plan)"
        f"{BACKEND_SHARD}.postgres_ports }}}}"
    )
    assert BACKEND_SHARD in backend["env"]["DATABASE_URL"]
    assert "db_host" in backend["env"]["DATABASE_URL"]
    for name in HOSTED_JOBS:
        assert "container" not in load_ci_jobs()[name]
        assert "services" not in load_ci_jobs()[name]

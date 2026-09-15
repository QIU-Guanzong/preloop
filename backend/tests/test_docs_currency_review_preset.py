"""Docs currency review preset (016), the fourth full-repo review lens.

The shared review-family skeleton is pinned in
test_repo_review_presets.py, which now parametrizes over this preset
too. This module pins what is specific to the docs currency lens:

* the five checkable claim types and nothing else — no finding about
  prose quality, tone, or completeness, ever;
* every claim carries a document pointer and a recorded search, and the
  fixture searches are re-run here against the fixture projects so a
  recorded classification cannot quietly stop being true;
* not checkable is reported with a reason and never counted as holding;
* the verdict rules, including the family rule that a positive row can
  never raise a verdict.

Deterministic: parses the shipped YAML and the synthetic fixtures under
fixtures/review/docs-currency. No agent, no network, no control plane.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

PRESETS_DIR = Path(__file__).resolve().parents[1] / "presets"
PRESET_FILE = "016-docs-currency-review.yaml"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "review" / "docs-currency"
PROJECTS_DIR = FIXTURES / "projects"
RESULTS_DIR = FIXTURES / "results"
SCHEMA_FILE = FIXTURES / "schemas" / "docscurrency-v1.json"

SCHEMA_ID = "preloop.review.docscurrency/v1"
FLOW_SLUG = "docs-currency-review"
PRESET_NAME = "Docs Currency Review"

CLAIM_TYPES = ("entry_point", "service", "dependency", "env_var", "command")

DISCLAIMER = (
    "Machine-generated review evidence. Not a certification, audit opinion, "
    "or legal advice."
)

# One scenario per acceptance criterion; the project is the input, the
# result is the contracted output for it.
SCENARIOS = {
    "drifted-build-command": "result-drifted-build-command.json",
    "accurate": "result-accurate.json",
    "not-checkable": "result-not-checkable.json",
    "prose-quality": "result-prose-quality.json",
}

# Vocabulary that would betray a prose-quality finding. This lens checks
# whether documentation is TRUE, never whether it is well written.
PROSE_WORDS = (
    "typo",
    "grammar",
    "spelling",
    "misspell",
    "readab",
    "rewrite",
    "reword",
    "rephrase",
    "tone",
    "style guide",
    "unclear",
    "poorly written",
    "badly written",
    "incomplete documentation",
)


def _norm(text: str) -> str:
    """Collapse whitespace so asserts survive YAML line wrapping."""
    return " ".join(text.split())


def _load_preset() -> dict:
    path = PRESETS_DIR / PRESET_FILE
    assert path.exists(), f"Missing preset file: {path}"
    data = yaml.safe_load(path.read_text())
    assert isinstance(data, dict)
    return data


def _prompt() -> str:
    return _load_preset()["prompt_template"]


def _required_shape_keys() -> list[str]:
    """Top-level keys of the YAML "Required shape" block (the contract)."""
    prompt = _prompt()
    marker = f"Required shape ({SCHEMA_ID}):"
    start = prompt.find(marker)
    assert start != -1, f"prompt missing {marker!r}"
    brace = prompt.find("{", start)
    depth = 0
    in_str = False
    keys: list[str] = []
    index = brace
    while index < len(prompt):
        char = prompt[index]
        if in_str:
            if char == "\\":
                index += 2
                continue
            if char == '"':
                in_str = False
            index += 1
            continue
        if char == '"':
            end = prompt.index('"', index + 1)
            if depth == 1 and prompt[end + 1 :].lstrip().startswith(":"):
                keys.append(prompt[index + 1 : end])
            index = end + 1
            continue
        if char in "{[":
            depth += 1
        elif char in "}]":
            depth -= 1
            if depth == 0:
                break
        index += 1
    assert keys, "Required shape had no top-level keys"
    return keys


def _load_result(project: str) -> dict:
    path = RESULTS_DIR / SCENARIOS[project]
    assert path.exists(), f"Missing result fixture: {path}"
    return json.loads(path.read_text())


def _project_files(project: str) -> list[tuple[str, Path]]:
    base = PROJECTS_DIR / project
    assert base.is_dir(), f"Missing fixture project: {base}"
    return sorted(
        (path.relative_to(base).as_posix(), path)
        for path in base.rglob("*")
        if path.is_file()
    )


def _in_scope(rel: str, scope: str) -> bool:
    """Whether a project-relative path belongs to a recorded search scope.

    ``.`` is the whole project. A scope that ends in ``/`` is a directory
    prefix. Any other scope is an exact file path, so ``widget`` does not
    match ``widget-plus/...`` and ``pyproject.toml`` does not match
    ``pyproject.toml.bak``.
    """
    if scope == ".":
        return True
    return rel.startswith(scope) if scope.endswith("/") else rel == scope


def _run_search(project: str, search: dict) -> list[str]:
    """Re-run a recorded search over a fixture project.

    ``filename:<path>`` is a lookup in the project file listing (a hit is
    ``<path>:0``, a file's existence is not a line); anything else is a
    regular expression matched line by line inside ``scope``.
    """
    pattern, scope = search["pattern"], search["scope"]
    files = [(rel, path) for rel, path in _project_files(project)]
    if pattern.startswith("filename:"):
        wanted = pattern.split(":", 1)[1]
        return [
            f"{rel}:0" for rel, _ in files if rel == wanted and _in_scope(rel, scope)
        ]
    regex = re.compile(pattern)
    hits: list[str] = []
    for rel, path in files:
        if not _in_scope(rel, scope):
            continue
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if regex.search(line):
                hits.append(f"{rel}:{number}")
    return hits


def _verdict(result: dict) -> str:
    """The verdict the preset's own rules compute for a result.

    fail when any claim drifted at high severity or the freeze floor did
    not hold; pass only when the plan completed with nothing drifted and
    nothing not checkable; everything else pass_with_findings.
    """
    claims = result["claims"]
    drifted = [claim for claim in claims if claim["status"] == "drifted"]
    unchecked = [claim for claim in claims if claim["status"] == "not_checkable"]
    drift = result.get("drift")
    floor_failed = bool(drift) and drift.get("freeze_floor_passed") is False
    if any(claim["severity"] == "high" for claim in drifted) or floor_failed:
        return "fail"
    if not drifted and not unchecked and result["coverage"]["plan_completed"]:
        return "pass"
    return "pass_with_findings"


def _holds_row(claim_id: str) -> dict:
    """A positive row: a claim the code confirms."""
    return {
        "id": claim_id,
        "type": "env_var",
        "claim": "EXAMPLE_TOKEN",
        "doc": "README.md:1",
        "status": "holds",
        "code_shows": "read by example/config.py",
        "evidence": "example/config.py:2",
        "search": {
            "pattern": "EXAMPLE_TOKEN",
            "scope": "example/",
            "matches": ["example/config.py:2"],
        },
        "severity": None,
        "reason": None,
    }


@pytest.fixture(params=sorted(SCENARIOS), ids=str)
def scenario(request) -> tuple[str, dict]:
    return request.param, _load_result(request.param)


class TestPresetDefinition:
    """What the preset file itself promises."""

    def test_identity_and_family_membership(self):
        data = _load_preset()
        assert data["slug"] == FLOW_SLUG
        assert data["name"] == PRESET_NAME
        assert data["is_preset"] is True
        assert data["icon"]
        assert SCHEMA_ID in data["description"]
        assert SCHEMA_ID in data["prompt_template"]
        assert f'"flow": "{FLOW_SLUG}"' in data["prompt_template"]

    def test_declares_no_write_tools(self):
        """Read-only like its siblings: no MCP server, no MCP tool, and a
        prompt that forbids every write path including writing docs."""
        data = _load_preset()
        assert data["allowed_mcp_servers"] == []
        assert data["allowed_mcp_tools"] == []
        norm = _norm(data["prompt_template"])
        assert "NO write tools" in norm
        assert "do not create issues, post comments, push commits" in norm
        assert "never run git commit or git push" in norm
        assert "Never modify tracked files" in norm
        assert "You never write, fix, or rephrase documentation" in norm

    def test_five_claim_types_and_no_sixth(self):
        prompt = _prompt()
        norm = _norm(prompt)
        assert "entry_point, service, dependency, env_var, command" in norm
        assert "|".join(CLAIM_TYPES) in prompt
        assert "Anything else a document says is OUT OF SCOPE" in norm

    def test_prose_quality_is_out_of_scope(self):
        """The lens that would become a prose critic if it were allowed
        to have an opinion about writing."""
        norm = _norm(_prompt())
        assert (
            "You NEVER emit a finding about prose quality, tone, spelling, "
            "grammar, structure, ordering, formatting, or completeness" in norm
        )
        assert "whose five claim types all hold is a PASS with zero findings" in norm
        assert "Missing documentation is not drift" in norm

    def test_claims_carry_document_pointers_and_recorded_searches(self):
        norm = _norm(_prompt())
        assert 'each with a "<path>:<line>" pointer into the document' in norm
        assert "checked against the code with a RECORDED SEARCH" in norm
        assert "A claim with no recorded search cannot be classified" in norm

    def test_not_checkable_is_never_a_hold(self):
        norm = _norm(_prompt())
        assert "NOT CHECKABLE IS NEVER COUNTED AS HOLDING" in norm
        assert "Record the reason" in norm
        assert 'not_checkable with reason "budget", never holds' in norm

    def test_severity_scale_is_drift_only(self):
        norm = _norm(_prompt())
        assert "Severity, only for drifted claims" in norm
        assert "does not exist" in norm
        assert "holds and not_checkable claims carry severity null" in norm
        assert "a document is never scored" in norm

    def test_project_scoping_inputs(self):
        """A project inside a larger repository, plus the family inputs."""
        prompt = _prompt()
        norm = _norm(prompt)
        assert "target_repo_path" in prompt
        assert "project_path" in prompt
        assert "one project in a repository that contains several" in norm
        assert "docs_paths" in prompt
        assert '"quick" | "standard" | "deep"' in prompt
        assert "previous_result_path" in prompt
        assert "previous_result_execution_id" in prompt
        assert "previous/result.json" in prompt
        assert "previous/baseline-mismatch.json" in prompt

    def test_register_grammar_maps_onto_the_family(self):
        norm = _norm(_prompt())
        assert "REGISTER GRAMMAR MAPPING" in norm
        assert (
            "holds is met, drifted is gap, and not_checkable is not_checkable" in norm
        )

    def test_verdict_rules_are_stated(self):
        norm = _norm(_prompt())
        assert (
            'Verdict: "fail" if any claim is drifted at high severity OR '
            "freeze_floor_passed is false" in norm
        )
        assert (
            '"pass" only when the plan completed, no claim is drifted, and no '
            "claim is not_checkable" in norm
        )
        assert "THE REGISTER CANNOT UPGRADE THE VERDICT" in norm
        assert "Documentation that reads well is not evidence of anything" in norm


class TestPresetLoadsIntoTheCatalogue:
    """The gallery entry: the loader must pick 016 up and expose it."""

    def test_preset_appears_in_the_catalogue_with_its_gallery_fields(self):
        from unittest.mock import patch

        from preloop.flow_presets import load_flow_presets

        load_flow_presets.cache_clear()
        try:
            with patch("preloop.flow_presets.PRESETS_DIRS", [PRESETS_DIR]):
                catalog = load_flow_presets()
        finally:
            load_flow_presets.cache_clear()

        entries = [entry for entry in catalog if entry["name"] == PRESET_NAME]
        assert len(entries) == 1, f"{PRESET_NAME} not in the catalogue exactly once"
        entry = entries[0]
        # What the picker renders.
        assert entry["description"]
        assert entry["icon"]
        assert entry["prompt_template"]
        assert entry["is_preset"] is True
        # Slug is loader-internal identity and never reaches consumers.
        assert "slug" not in entry


class TestResultSchema:
    """The new schema, and the fixtures that have to satisfy it."""

    def test_schema_is_a_valid_json_schema(self):
        schema = json.loads(SCHEMA_FILE.read_text())
        Draft202012Validator.check_schema(schema)

    def test_schema_required_matches_the_yaml_contract(self):
        schema = json.loads(SCHEMA_FILE.read_text())
        assert schema["required"] == _required_shape_keys()
        extra = set(schema["properties"]) - set(_required_shape_keys())
        assert extra == set(), f"schema invents fields the YAML does not name: {extra}"

    def test_result_validates_against_the_schema(self, scenario):
        _, result = scenario
        schema = json.loads(SCHEMA_FILE.read_text())
        Draft202012Validator(schema).validate(result)

    def test_result_carries_every_required_key(self, scenario):
        _, result = scenario
        missing = [key for key in _required_shape_keys() if key not in result]
        assert missing == [], f"result fixture missing required keys: {missing}"
        assert result["schema"] == SCHEMA_ID
        assert result["flow"] == FLOW_SLUG
        assert result["status"] == "success"
        assert result["disclaimer"] == DISCLAIMER

    def test_fixtures_are_synthetic(self, scenario):
        project, result = scenario
        assert result["note"] == "synthetic fixture"
        blob = "\n".join(
            path.read_text() for _, path in _project_files(project)
        ).lower()
        assert "synthetic fixture" in blob


class TestClaimDiscipline:
    """Invariants every run has to hold, whatever the project."""

    def test_only_the_five_supported_claim_types_appear(self, scenario):
        _, result = scenario
        types = {claim["type"] for claim in result["claims"]}
        assert types <= set(CLAIM_TYPES), f"unsupported claim types: {types}"

    def test_every_claim_points_at_the_document_line_it_quotes(self, scenario):
        project, result = scenario
        for claim in result["claims"]:
            path, _, line_no = claim["doc"].rpartition(":")
            document = PROJECTS_DIR / project / path
            assert document.exists(), f"{claim['id']}: no such document {path}"
            lines = document.read_text().splitlines()
            index = int(line_no)
            assert 1 <= index <= len(lines), f"{claim['id']}: line {index} out of range"
            assert claim["claim"] in lines[index - 1], (
                f"{claim['id']}: {claim['claim']!r} is not on {claim['doc']}"
            )

    def test_every_claim_records_a_search_that_still_reproduces(self, scenario):
        project, result = scenario
        for claim in result["claims"]:
            search = claim["search"]
            assert search["pattern"] and search["scope"]
            assert _run_search(project, search) == search["matches"], (
                f"{claim['id']}: recorded search no longer reproduces"
            )

    def test_counts_agree_with_the_claim_rows(self, scenario):
        _, result = scenario
        claims = result["claims"]
        counts = result["counts"]
        for status in ("holds", "drifted", "not_checkable"):
            assert counts[status] == sum(1 for c in claims if c["status"] == status)
        for level in ("high", "medium", "low"):
            assert counts["by_severity"][level] == sum(
                1 for c in claims if c["severity"] == level
            )
        for kind in CLAIM_TYPES:
            assert counts["by_type"][kind] == sum(
                1 for c in claims if c["type"] == kind
            )
        assert result["coverage"]["claims_extracted"] == len(claims)

    def test_the_verdict_is_the_one_the_rules_compute(self, scenario):
        _, result = scenario
        assert result["verdict"] == _verdict(result)


class TestDriftedBuildCommandScenario:
    """A README naming a build command the project does not have."""

    @pytest.fixture()
    def result(self) -> dict:
        return _load_result("drifted-build-command")

    def test_one_high_severity_drift_row_with_pointer_and_search(self, result):
        drifted = [c for c in result["claims"] if c["status"] == "drifted"]
        assert len(drifted) == 1
        row = drifted[0]
        assert row["severity"] == "high"
        assert row["type"] == "command"
        assert row["claim"] == "make build"
        assert row["doc"] == "README.md:16"
        assert row["search"]["pattern"] == "^build:"
        assert row["code_shows"]
        # The absence claim is backed by the search: nothing matched.
        assert row["search"]["matches"] == []
        assert _run_search("drifted-build-command", row["search"]) == []

    def test_the_verdict_is_a_failure(self, result):
        assert result["verdict"] == "fail"
        assert result["status"] == "success"  # a failing review still completed

    def test_the_documented_command_really_is_missing_from_the_project(self, result):
        makefile = (PROJECTS_DIR / "drifted-build-command" / "Makefile").read_text()
        assert "build:" not in makefile
        assert "test:" in makefile


class TestAccurateScenario:
    """Documentation that still matches the code."""

    @pytest.fixture()
    def result(self) -> dict:
        return _load_result("accurate")

    def test_no_drift_rows_and_a_passing_verdict(self, result):
        assert [c for c in result["claims"] if c["status"] != "holds"] == []
        assert result["counts"]["drifted"] == 0
        assert result["counts"]["not_checkable"] == 0
        assert result["verdict"] == "pass"

    def test_all_five_claim_types_are_exercised(self, result):
        assert {c["type"] for c in result["claims"]} == set(CLAIM_TYPES)


class TestNotCheckableScenario:
    """Claims this checkout cannot decide."""

    @pytest.fixture()
    def result(self) -> dict:
        return _load_result("not-checkable")

    def test_unverifiable_claims_carry_a_reason(self, result):
        unchecked = [c for c in result["claims"] if c["status"] == "not_checkable"]
        assert len(unchecked) == 2
        for row in unchecked:
            assert row["reason"], f"{row['id']}: not_checkable without a reason"
            assert row["severity"] is None
            assert row["evidence"] is None

    def test_not_checkable_is_never_counted_as_holding(self, result):
        unchecked = {
            c["id"] for c in result["claims"] if c["status"] == "not_checkable"
        }
        holds = {c["id"] for c in result["claims"] if c["status"] == "holds"}
        assert unchecked & holds == set()
        assert result["counts"]["holds"] == len(holds)
        assert result["counts"]["not_checkable"] == len(unchecked)

    def test_it_is_not_a_pass(self, result):
        """A claim nobody could check is a finding, not a clean bill."""
        assert result["verdict"] == "pass_with_findings"


class TestProseQualityScenario:
    """Badly written but accurate documentation."""

    @pytest.fixture()
    def result(self) -> dict:
        return _load_result("prose-quality")

    def test_the_fixture_really_is_badly_written(self):
        readme = (PROJECTS_DIR / "prose-quality" / "README.md").read_text()
        assert "basicaly" in readme  # spelling
        assert "depedencies" in readme  # spelling

    def test_no_finding_is_emitted_about_prose_quality(self, result):
        for claim in result["claims"]:
            assert claim["status"] == "holds", (
                f"{claim['id']}: badly written prose produced a finding"
            )
            blob = json.dumps(claim).lower()
            hits = [word for word in PROSE_WORDS if word in blob]
            assert hits == [], f"{claim['id']}: prose-quality vocabulary {hits}"

    def test_accurate_documentation_passes_however_it_reads(self, result):
        assert result["counts"]["drifted"] == 0
        assert result["verdict"] == "pass"


class TestVerdictHonesty:
    """The family rule: a positive row can never raise a verdict."""

    def test_adding_holds_rows_cannot_rescue_a_failing_run(self):
        result = _load_result("drifted-build-command")
        assert _verdict(result) == "fail"
        result["claims"].extend(
            _holds_row(f"docs:env_var:README.md:extra-{n}") for n in range(20)
        )
        assert _verdict(result) == "fail"

    def test_adding_holds_rows_cannot_clear_a_not_checkable_claim(self):
        result = _load_result("not-checkable")
        assert _verdict(result) == "pass_with_findings"
        result["claims"].extend(
            _holds_row(f"docs:env_var:README.md:extra-{n}") for n in range(5)
        )
        assert _verdict(result) == "pass_with_findings"

    def test_a_failed_freeze_floor_fails_the_run_on_its_own(self):
        result = _load_result("accurate")
        assert _verdict(result) == "pass"
        result["drift"] = {
            "baseline": {"schema": SCHEMA_ID, "run_at": None, "commit": None},
            "baseline_mismatch": False,
            "new": [],
            "persisting": [],
            "resolved": [],
            "freeze_floor_passed": False,
        }
        assert _verdict(result) == "fail"

    def test_incomplete_coverage_cannot_pass(self):
        result = _load_result("accurate")
        result["coverage"]["plan_completed"] = False
        result["coverage"]["not_reviewed"] = ["docs/operations.md"]
        assert _verdict(result) == "pass_with_findings"


class TestSearchScope:
    """Directory scopes need a trailing slash; file scopes are exact."""

    def test_dot_matches_every_path(self) -> None:
        assert _in_scope("widget/server.py", ".")
        assert _in_scope("pyproject.toml", ".")

    def test_directory_prefix_requires_a_trailing_slash(self) -> None:
        assert _in_scope("widget/server.py", "widget/")
        assert not _in_scope("widget/server.py", "widget")
        assert not _in_scope("widget-plus/cli.py", "widget/")
        assert not _in_scope("widget-plus/cli.py", "widget")

    def test_file_scope_is_exact(self) -> None:
        assert _in_scope("pyproject.toml", "pyproject.toml")
        assert not _in_scope("pyproject.toml.bak", "pyproject.toml")

    def test_run_search_directory_scope_does_not_bleed_into_siblings(self) -> None:
        hits = _run_search(
            "drifted-build-command",
            {"pattern": "filename:widget/server.py", "scope": "widget/"},
        )
        assert hits == ["widget/server.py:0"]
        assert (
            _run_search(
                "drifted-build-command",
                {"pattern": "filename:widget/server.py", "scope": "widget"},
            )
            == []
        )

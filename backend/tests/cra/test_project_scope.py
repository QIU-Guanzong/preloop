"""Release security audit scoped to one project inside a repository.

The portfolio review runs this preset once per project instead of forking
it, so the audit has to be tellable which project it is auditing. These
tests pin the contract that makes that honest:

- with no project path, nothing changes (the shipped fixture still
  validates, with or without the new field);
- with a project path, every evidence pointer is inside it and another
  project's SBOM is not this project's evidence;
- a project with no SBOM of its own is ``not_checkable`` with a reason,
  never a clean bill of health.

``not_checkable`` is the review family's word for an absence of evidence
(``docs/guide/flows/repo-review-presets.md``). The assertions run against
the serialised envelope so a later rename to "skipped" cannot pass
silently.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from preloop.cra.schemas import (
    SCHEMA_RELEASEAUDIT_V1,
    SCHEMA_VULNSCAN_V1,
)
from preloop.cra.validate import validate_cra_result

from .conftest import clone

PROJECT = "projects/device-gateway"
DISCLAIMER = (
    "Machine-generated evidence for conformity assessment support. "
    "Not a conformity assessment, certification, or legal advice."
)


def _gap_register(*, evidence: str, secret_path: str) -> dict[str, Any]:
    return {
        "ran": True,
        "repo": {
            "remote": "https://github.com/example/portfolio.git",
            "commit": "1" * 40,
            "branch": "main",
        },
        "items": [
            {
                "id": "cvd_policy",
                "title": "Coordinated vulnerability disclosure policy",
                "status": "met",
                "evidence": evidence,
            }
        ],
        "secrets_findings": [
            {
                "sha": "a" * 40,
                "path": secret_path,
                "subject": "remove committed credential",
                "term": "API_TOKEN",
                "kind": "history",
                "status": "finding",
            }
        ],
        "secrets_findings_count": 1,
        "not_checkable": ["support window is declared repository wide"],
        "resolved": [],
        "ready": False,
    }


def _scoped(
    result: dict[str, Any],
    *,
    project_path: str | None = PROJECT,
    evidence: str = f"{PROJECT}/SECURITY.md:1",
    secret_path: str = f"{PROJECT}/config/broker.h",
    **scope: Any,
) -> dict[str, Any]:
    payload = clone(result)
    payload["scope"] = {
        "project_path": project_path,
        "covers": "project" if project_path else "repository",
        "status": "audited",
        "sbom_paths": [f"{project_path}/sbom/image.spdx.json"] if project_path else [],
        **scope,
    }
    payload["gap_register"] = _gap_register(evidence=evidence, secret_path=secret_path)
    payload["artifacts"]["gap_register"] = "evidence/gap-register.md"
    return payload


def _not_checkable_envelope(**overrides: Any) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "schema": SCHEMA_RELEASEAUDIT_V1,
        "flow": "release-security-audit",
        "run_at": "2026-09-15T10:00:00Z",
        "regime_profile": "cra",
        "verdict": "error",
        "incomplete": {
            "reason": (
                f"no SBOM was delivered for {PROJECT}; this family verifies "
                "SBOMs and never generates one"
            ),
            "stage": "PHASE 0",
        },
        "scope": {
            "project_path": PROJECT,
            "covers": "project",
            "status": "not_checkable",
            "reason": "no SBOM available",
        },
        "disclaimer": DISCLAIMER,
    }
    envelope.update(overrides)
    return envelope


def _validate(payload: dict[str, Any]):
    return validate_cra_result(payload, expected_schema=SCHEMA_RELEASEAUDIT_V1)


class TestNoProjectPathIsUnchanged:
    """Whole-repository runs behave exactly as they did before."""

    def test_shipped_fixture_still_validates(self, releaseaudit_result):
        result = _validate(releaseaudit_result)
        assert result.ok, result.failures
        assert result.execution_completed is True
        assert releaseaudit_result["scope"] is None

    def test_result_written_before_the_field_existed_still_validates(
        self, releaseaudit_result
    ):
        """The block is additive: an older result has no ``scope`` key."""
        legacy = clone(releaseaudit_result)
        legacy.pop("scope")
        legacy_result = _validate(legacy)
        current = _validate(releaseaudit_result)
        assert legacy_result.ok, legacy_result.failures
        assert legacy_result.execution_completed == current.execution_completed
        assert legacy_result.release_denied == current.release_denied
        assert legacy_result.failures == current.failures == []

    def test_covers_repository_may_not_name_a_project_path(self, releaseaudit_result):
        payload = clone(releaseaudit_result)
        payload["scope"] = {
            "project_path": PROJECT,
            "covers": "repository",
            "status": "audited",
        }
        result = _validate(payload)
        assert not result.ok
        assert any("covers is repository" in failure for failure in result.failures)


class TestScopedRunRecordsItsScope:
    def test_audited_scope_is_in_the_serialised_envelope(self, releaseaudit_result):
        payload = _scoped(releaseaudit_result)
        result = _validate(payload)
        assert result.ok, result.failures
        serialised = json.loads(json.dumps(payload))
        assert serialised["scope"]["project_path"] == PROJECT
        assert serialised["scope"]["covers"] == "project"
        assert serialised["scope"]["status"] == "audited"

    def test_project_path_may_not_escape_the_repository(self, releaseaudit_result):
        payload = _scoped(releaseaudit_result, project_path="../other-repo")
        result = _validate(payload)
        assert not result.ok
        assert any("project_path" in failure for failure in result.failures)

    def test_covers_project_needs_a_path(self, releaseaudit_result):
        payload = clone(releaseaudit_result)
        payload["scope"] = {
            "project_path": None,
            "covers": "project",
            "status": "audited",
        }
        result = _validate(payload)
        assert not result.ok
        assert any("names no path" in failure for failure in result.failures)


class TestEveryPointerIsInsideTheProject:
    def test_pointers_inside_the_project_pass(self, releaseaudit_result):
        payload = _scoped(releaseaudit_result)
        result = _validate(payload)
        assert result.ok, result.failures

    def test_gap_item_pointer_outside_the_project_fails(self, releaseaudit_result):
        payload = _scoped(releaseaudit_result, evidence="SECURITY.md:1")
        result = _validate(payload)
        assert not result.ok
        assert any(
            "outside the audited scope" in failure for failure in result.failures
        )

    def test_sibling_project_pointer_fails(self, releaseaudit_result):
        payload = _scoped(
            releaseaudit_result, evidence="projects/other-app/SECURITY.md:4"
        )
        result = _validate(payload)
        assert not result.ok
        assert any(
            "projects/other-app/SECURITY.md:4" in failure for failure in result.failures
        )

    def test_secrets_finding_path_outside_the_project_fails(self, releaseaudit_result):
        payload = _scoped(
            releaseaudit_result, secret_path="projects/other-app/config/broker.h"
        )
        result = _validate(payload)
        assert not result.ok
        assert any("secrets_findings[0].path" in failure for failure in result.failures)

    def test_commit_sha_evidence_is_not_a_path(self, releaseaudit_result):
        """A commit SHA is a pointer into history, not into the tree."""
        payload = _scoped(releaseaudit_result, evidence="b" * 40)
        result = _validate(payload)
        assert result.ok, result.failures

    def test_prose_evidence_is_left_alone(self, releaseaudit_result):
        payload = _scoped(
            releaseaudit_result, evidence="no SECURITY.md under the project at HEAD"
        )
        result = _validate(payload)
        assert result.ok, result.failures


class TestAnotherProjectsSbomIsNotEvidence:
    def test_sbom_path_outside_the_project_fails(self, releaseaudit_result):
        payload = _scoped(releaseaudit_result)
        payload["scope"]["sbom_paths"] = ["projects/other-app/sbom/image.spdx.json"]
        result = _validate(payload)
        assert not result.ok
        assert any(
            "another project's SBOM is not this project's evidence" in failure
            for failure in result.failures
        )

    def test_not_checkable_cannot_also_name_an_sbom(self):
        envelope = _not_checkable_envelope()
        envelope["scope"]["sbom_paths"] = ["projects/other-app/sbom/image.spdx.json"]
        result = _validate(envelope)
        assert not result.ok
        assert any("not_checkable" in failure for failure in result.failures)


class TestNotCheckableLens:
    def test_no_sbom_for_the_project_is_not_checkable_with_a_reason(self):
        envelope = _not_checkable_envelope()
        result = _validate(envelope)
        assert result.ok, result.failures
        assert result.incomplete is True
        assert result.execution_completed is False
        assert result.release_denied is True
        serialised = json.dumps(envelope)
        assert '"not_checkable"' in serialised
        assert "skipped" not in serialised
        assert json.loads(serialised)["scope"]["reason"] == "no SBOM available"

    def test_reason_is_required(self):
        envelope = _not_checkable_envelope()
        envelope["scope"].pop("reason")
        result = _validate(envelope)
        assert not result.ok
        assert any("scope.reason must say why" in f for f in result.failures)

    def test_empty_reason_is_not_a_reason(self):
        envelope = _not_checkable_envelope()
        envelope["scope"]["reason"] = "   "
        result = _validate(envelope)
        assert not result.ok

    def test_a_rename_to_skipped_does_not_pass_silently(self):
        """The literal string is ``not_checkable``; ``skipped`` is rejected."""
        envelope = _not_checkable_envelope()
        envelope["scope"]["status"] = "skipped"
        result = _validate(envelope)
        assert not result.ok
        assert any("status must be audited|not_checkable" in f for f in result.failures)

    @pytest.mark.parametrize("verdict", ["pass", "pass_with_findings"])
    def test_not_checkable_never_yields_a_healthy_verdict(
        self, releaseaudit_result, verdict
    ):
        payload = clone(releaseaudit_result)
        payload["verdict"] = verdict
        payload["vuln_scan"]["findings"] = []
        payload["checks"] = []
        payload["scope"] = {
            "project_path": PROJECT,
            "covers": "project",
            "status": "not_checkable",
            "reason": "no SBOM available",
        }
        result = _validate(payload)
        assert not result.ok
        assert any("cannot pass" in failure for failure in result.failures)

    def test_not_checkable_may_not_carry_an_audit_body(self, releaseaudit_result):
        payload = clone(releaseaudit_result)
        payload["verdict"] = "fail"
        payload["sbom_audit"]["verdict"] = "fail"
        payload["scope"] = {
            "project_path": PROJECT,
            "covers": "project",
            "status": "not_checkable",
            "reason": "no SBOM available",
        }
        result = _validate(payload)
        assert not result.ok
        assert any("carries an audit body" in failure for failure in result.failures)


class TestScopeIsReleaseAuditOnly:
    def test_other_schemas_do_not_carry_a_scope_block(self):
        envelope = {
            "schema": SCHEMA_VULNSCAN_V1,
            "flow": "sbom-exploit-check",
            "run_at": "2026-09-15T10:00:00Z",
            "regime_profile": "cra",
            "status": "error",
            "incomplete": {"reason": "no SBOM delivered", "stage": "PHASE 0"},
            "scope": {
                "project_path": PROJECT,
                "covers": "project",
                "status": "not_checkable",
                "reason": "no SBOM available",
            },
            "disclaimer": DISCLAIMER,
        }
        result = validate_cra_result(envelope, expected_schema=SCHEMA_VULNSCAN_V1)
        assert not result.ok
        assert any("scope is not part of" in failure for failure in result.failures)

"""Tests for the previous-result baseline transport (utils.workspace_baseline).

The resolution half (which execution, in which account) lives in
tests/services/test_workspace_baseline.py; this file pins the payload key
lookup, the precedence rule, the chunked environment transport, and what
the emitted shell block actually writes into a workspace.
"""

import base64
import json
import os
import subprocess

import pytest
from pydantic import ValidationError

from preloop.utils.workspace_baseline import (
    BASELINE_CHUNK_ENCODED_BYTES,
    BASELINE_MISMATCH_PATH,
    BASELINE_WORKSPACE_PATH,
    MISMATCH_NO_RESULT,
    MISMATCH_UNAVAILABLE,
    PREVIOUS_RUN_SENTINEL,
    BaselineDelivery,
    baseline_chunk_env_var,
    baseline_chunks,
    baseline_delivery,
    baseline_env,
    baseline_mismatch,
    build_workspace_baseline_shell,
    explicit_baseline_declared,
    previous_result_execution_id,
    serialize_baseline,
)
from preloop.utils.workspace_seed import workspace_containment_shell_body

EXECUTION_ID = "0f1d4c0e-7a1c-4a9a-9a8f-2f0b1d4c0e7a"


class TestPayloadKeyLookup:
    """Where the key may live, and what counts as a request."""

    def test_absent_key_requests_nothing(self):
        assert previous_result_execution_id({"payload": {"depth": "quick"}}) is None
        assert previous_result_execution_id({}) is None
        assert previous_result_execution_id(None) is None

    def test_inside_payload(self):
        body = {"payload": {"previous_result_execution_id": EXECUTION_ID}}
        assert previous_result_execution_id(body) == EXECUTION_ID

    def test_beside_payload(self):
        body = {"payload": {}, "previous_result_execution_id": EXECUTION_ID}
        assert previous_result_execution_id(body) == EXECUTION_ID

    def test_inside_payload_wins_over_top_level(self):
        body = {
            "payload": {"previous_result_execution_id": EXECUTION_ID},
            "previous_result_execution_id": "last",
        }
        assert previous_result_execution_id(body) == EXECUTION_ID

    def test_sentinel_is_passed_through(self):
        body = {"payload": {"previous_result_execution_id": PREVIOUS_RUN_SENTINEL}}
        assert previous_result_execution_id(body) == "last"

    def test_surrounding_whitespace_stripped(self):
        body = {"payload": {"previous_result_execution_id": f"  {EXECUTION_ID} "}}
        assert previous_result_execution_id(body) == EXECUTION_ID

    def test_non_string_is_requested_but_unusable(self):
        """A dict is not an id: the run gets a marker, not silence."""
        body = {"payload": {"previous_result_execution_id": {"id": EXECUTION_ID}}}
        assert previous_result_execution_id(body) == ""


class TestExplicitBaselinePrecedence:
    """An explicitly delivered baseline file wins over the execution id."""

    def test_nothing_declared(self):
        assert not explicit_baseline_declared({"payload": {}}, [])

    def test_previous_result_path_declared(self):
        body = {"payload": {"previous_result_path": "previous/result.json"}}
        assert explicit_baseline_declared(body, [])

    def test_previous_result_url_declared(self):
        body = {"payload": {"previous_result_url": "https://example.com/result.json"}}
        assert explicit_baseline_declared(body, [])

    def test_seeded_file_at_the_baseline_path(self):
        assert explicit_baseline_declared({"payload": {}}, [BASELINE_WORKSPACE_PATH])

    def test_seed_declaration_beside_the_payload(self):
        body = {
            "payload": {},
            "workspace_files": [
                {"path": BASELINE_WORKSPACE_PATH, "content_base64": "e30="}
            ],
        }
        assert explicit_baseline_declared(body, [])

    def test_unrelated_seed_does_not_claim_precedence(self):
        assert not explicit_baseline_declared({"payload": {}}, ["fixtures/input.json"])


class TestChunkedEnvironment:
    """A result envelope does not fit one execve string, so it is chunked."""

    def test_small_baseline_is_one_chunk(self):
        delivery = baseline_delivery(b'{"findings": []}')
        env = baseline_env(delivery)
        assert list(env) == [baseline_chunk_env_var(0)]
        assert base64.b64decode(env[baseline_chunk_env_var(0)]) == b'{"findings": []}'

    def test_large_baseline_splits_into_ordered_chunks(self):
        content = json.dumps({"items": ["x" * 40] * 4000}).encode("utf-8")
        delivery = baseline_delivery(content)
        env = baseline_env(delivery)
        assert len(env) > 1
        for name, chunk in env.items():
            assert len(chunk) <= BASELINE_CHUNK_ENCODED_BYTES, name
        rejoined = "".join(
            env[baseline_chunk_env_var(index)] for index in range(len(env))
        )
        assert base64.b64decode(rejoined) == content

    def test_chunk_boundaries_reproduce_the_encoded_text(self):
        encoded = base64.b64encode(b"y" * (BASELINE_CHUNK_ENCODED_BYTES * 2)).decode()
        assert "".join(baseline_chunks(encoded)) == encoded

    def test_marker_carries_no_environment(self):
        assert baseline_env(baseline_mismatch(MISMATCH_UNAVAILABLE)) == {}

    def test_no_delivery_carries_no_environment(self):
        assert baseline_env(None) == {}

    def test_baseline_bytes_never_enter_the_launch_command(self):
        content = b'{"secretish": "' + b"z" * 5000 + b'"}'
        shell = build_workspace_baseline_shell(baseline_delivery(content))
        assert "z" * 100 not in shell
        assert base64.b64encode(content).decode()[:200] not in shell


class TestSerialization:
    """What gets written is the stored result, verbatim JSON."""

    def test_round_trips(self):
        result = {"schema": "preloop.review.codehealth/v1", "findings": [{"id": "a"}]}
        assert json.loads(serialize_baseline(result)) == result

    def test_exotic_values_do_not_raise(self):
        from datetime import datetime

        assert serialize_baseline({"at": datetime(2026, 9, 15)})


class TestDeliveryInvariant:
    """Never both, never neither: empty content with no reason is unusable."""

    def test_neither_is_rejected_at_model_validate(self):
        with pytest.raises(ValidationError, match="never neither"):
            BaselineDelivery.model_validate(
                {"content_base64": "", "mismatch_reason": None}
            )

    def test_default_construction_is_neither(self):
        with pytest.raises(ValidationError, match="never neither"):
            BaselineDelivery()

    def test_both_is_rejected(self):
        with pytest.raises(ValidationError, match="exactly one"):
            BaselineDelivery(
                content_base64="e30=", mismatch_reason=MISMATCH_UNAVAILABLE
            )

    def test_mismatch_with_empty_content_is_valid(self):
        delivery = BaselineDelivery(mismatch_reason=MISMATCH_UNAVAILABLE)
        assert not delivery.delivered
        assert delivery.content_base64 == ""

    def test_content_without_a_reason_is_valid(self):
        delivery = BaselineDelivery(content_base64="e30=")
        assert delivery.delivered


class TestSharedContainmentGuard:
    """The baseline dir helper is the seed module's guard, not a second copy."""

    def test_baseline_helper_uses_the_shared_body(self):
        from preloop.utils.workspace_baseline import _containment_helper

        body = workspace_containment_shell_body("baseline")
        assert body in _containment_helper()


class TestBaselineMaterialization:
    """What the emitted shell block writes into a real directory."""

    _env: dict = {}

    def _run(self, shell: str):
        return subprocess.run(
            ["sh", "-c", shell],
            capture_output=True,
            text=True,
            env={**os.environ, **self._env},
        )

    def _shell_for(self, root, delivery) -> str:
        self._env = baseline_env(delivery)
        return build_workspace_baseline_shell(delivery, workspace_root=str(root))

    def test_no_delivery_emits_nothing(self):
        assert build_workspace_baseline_shell(None) == ""

    def test_baseline_lands_at_the_documented_path(self, tmp_path):
        result = {"schema": "preloop.review.codehealth/v1", "findings": ["one"]}
        delivery = baseline_delivery(
            serialize_baseline(result), source_execution_id=EXECUTION_ID
        )
        outcome = self._run(self._shell_for(tmp_path, delivery))

        assert outcome.returncode == 0, outcome.stderr
        written = tmp_path / BASELINE_WORKSPACE_PATH
        assert json.loads(written.read_text()) == result
        assert not (tmp_path / BASELINE_MISMATCH_PATH).exists()

    def test_multi_chunk_baseline_is_reassembled_in_order(self, tmp_path):
        result = {"findings": [{"id": f"health:{index}"} for index in range(6000)]}
        delivery = baseline_delivery(serialize_baseline(result))
        assert len(baseline_env(delivery)) > 1

        outcome = self._run(self._shell_for(tmp_path, delivery))

        assert outcome.returncode == 0, outcome.stderr
        assert json.loads((tmp_path / BASELINE_WORKSPACE_PATH).read_text()) == result

    def test_dropped_chunk_aborts_instead_of_writing_half_a_baseline(self, tmp_path):
        result = {"findings": [{"id": f"health:{index}"} for index in range(6000)]}
        delivery = baseline_delivery(serialize_baseline(result))
        shell = self._shell_for(tmp_path, delivery)
        # Simulate a transport that drops the last variable.
        last = baseline_chunk_env_var(len(self._env) - 1)
        self._env[last] = ""

        outcome = self._run(shell)

        assert outcome.returncode != 0
        assert "not delivered" in outcome.stderr
        assert not (tmp_path / BASELINE_WORKSPACE_PATH).exists()

    def test_mismatch_marker_names_the_reason(self, tmp_path):
        outcome = self._run(
            self._shell_for(tmp_path, baseline_mismatch(MISMATCH_NO_RESULT))
        )

        assert outcome.returncode == 0, outcome.stderr
        marker = json.loads((tmp_path / BASELINE_MISMATCH_PATH).read_text())
        assert marker == {
            "baseline_mismatch": True,
            "reason": MISMATCH_NO_RESULT,
        }
        assert not (tmp_path / BASELINE_WORKSPACE_PATH).exists()

    def test_marker_clears_a_stale_baseline_from_a_restored_workspace(self, tmp_path):
        stale = tmp_path / BASELINE_WORKSPACE_PATH
        stale.parent.mkdir(parents=True)
        stale.write_text('{"findings": ["stale"]}')

        outcome = self._run(
            self._shell_for(tmp_path, baseline_mismatch(MISMATCH_UNAVAILABLE))
        )

        assert outcome.returncode == 0, outcome.stderr
        assert not stale.exists()

    def test_baseline_clears_a_stale_marker(self, tmp_path):
        marker = tmp_path / BASELINE_MISMATCH_PATH
        marker.parent.mkdir(parents=True)
        marker.write_text('{"baseline_mismatch": true, "reason": "stale"}')

        delivery = baseline_delivery(serialize_baseline({"findings": []}))
        outcome = self._run(self._shell_for(tmp_path, delivery))

        assert outcome.returncode == 0, outcome.stderr
        assert not marker.exists()

    def test_symlinked_parent_escape_refused(self, tmp_path):
        """A cloned repo may contain `previous -> <outside>`."""
        workspace = tmp_path / "workspace"
        outside = tmp_path / "outside"
        workspace.mkdir()
        outside.mkdir()
        (workspace / "previous").symlink_to(outside)

        delivery = baseline_delivery(serialize_baseline({"findings": []}))
        outcome = self._run(self._shell_for(workspace, delivery))

        assert outcome.returncode != 0
        assert "resolves outside" in outcome.stderr
        assert not (outside / "result.json").exists()

    def test_symlinked_target_refused(self, tmp_path):
        outside = tmp_path / "outside.json"
        outside.write_text("untouched")
        (tmp_path / "previous").mkdir()
        (tmp_path / BASELINE_WORKSPACE_PATH).symlink_to(outside)

        delivery = baseline_delivery(serialize_baseline({"findings": []}))
        outcome = self._run(self._shell_for(tmp_path, delivery))

        assert outcome.returncode != 0
        assert "symlink" in outcome.stderr
        assert outside.read_text() == "untouched"

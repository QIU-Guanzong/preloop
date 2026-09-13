"""Regression coverage for handoff state lost around runner completion."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.services import flow_pr_binding as binding
from preloop.services.flow_trigger_service import FlowTriggerService


def legacy_binding() -> tuple[SimpleNamespace, SimpleNamespace, dict]:
    account, tracker, flow_id = uuid4(), uuid4(), uuid4()
    flow = SimpleNamespace(
        id=flow_id,
        account_id=account,
        trigger_event_source=str(tracker),
        trigger_event_types=["issue_labeled", "comment_created"],
        trigger_config={"labels": ["agent-ready"]},
        name="Issue implementation",
    )
    identity = {
        "source": "github",
        "account_id": str(account),
        "tracker_id": str(tracker),
        "payload": {"repository": {"id": 123}},
    }
    prior = SimpleNamespace(
        id=uuid4(),
        flow_id=flow_id,
        status="FAILED",
        trigger_event_details=deepcopy(identity),
        result={"pr_url": "https://github.com/example/repo/pull/42"},
    )
    event = {
        **identity,
        "type": "comment_created",
        "payload": {
            **identity["payload"],
            "issue": {
                "labels": [],
                "pull_request": {"html_url": prior.result["pr_url"]},
            },
        },
    }
    return flow, prior, event


def test_bound_failed_implementation_comment_does_not_need_issue_label() -> None:
    flow, prior, event = legacy_binding()
    with patch.object(binding, "find_bound_execution", return_value=prior):
        assert FlowTriggerService(MagicMock()).matches_trigger_config(flow, event)


@pytest.mark.parametrize(
    "mismatch",
    ["account", "tracker", "repository", "event", "unbound", "flow", "intake_only"],
)
def test_only_explicitly_enabled_same_repository_handoff_bypasses_labels(
    mismatch: str,
) -> None:
    flow, prior, event = legacy_binding()
    if mismatch == "account":
        event["account_id"] = str(uuid4())
    elif mismatch == "tracker":
        event["tracker_id"] = str(uuid4())
    elif mismatch == "repository":
        event["payload"]["repository"] = {"id": 999}
    elif mismatch == "event":
        event["type"] = "issue_labeled"
    elif mismatch == "flow":
        prior.flow_id = uuid4()
    elif mismatch == "intake_only":
        flow.trigger_event_types = ["issue_labeled"]
    with patch.object(
        binding,
        "find_bound_execution",
        return_value=None if mismatch == "unbound" else prior,
    ):
        assert not FlowTriggerService(MagicMock()).matches_trigger_config(flow, event)


def test_bound_comment_still_obeys_non_label_filters() -> None:
    flow, prior, event = legacy_binding()
    flow.trigger_config = {
        "filter_conditions": {"labels": ["agent-ready"], "approved": True}
    }
    with patch.object(binding, "find_bound_execution", return_value=prior):
        assert not FlowTriggerService(MagicMock()).matches_trigger_config(flow, event)


def test_runner_marker_persists_pr_before_completion() -> None:
    flow, prior, event = legacy_binding()
    prior.result = None
    prior.cli_session = None
    marker = 'PRELOOP_PR_OPENED {"url":"https://github.com/example/repo/pull/42","branch":"fix/42","provider":"github"}'
    with patch.object(binding, "record_opened_pr") as record:
        binding.record_runner_handoff_markers(
            MagicMock(), prior, marker, isolated_publication=False
        )
    record.assert_called_once_with(
        ANY,
        prior.id,
        "https://github.com/example/repo/pull/42",
        source_branch="fix/42",
        raise_errors=True,
    )


def test_runner_marker_cannot_bind_isolated_publication() -> None:
    _, prior, _ = legacy_binding()
    with patch.object(binding, "record_opened_pr") as record:
        binding.record_runner_handoff_markers(
            MagicMock(),
            prior,
            'PRELOOP_PR_OPENED {"url":"https://github.com/example/repo/pull/42"}',
            isolated_publication=True,
        )
    record.assert_not_called()


def test_runner_native_identity_survives_without_completion() -> None:
    _, prior, _ = legacy_binding()
    prior.cli_session = None
    session_id = str(uuid4())
    with patch.object(binding, "record_cli_session") as record:
        binding.record_runner_handoff_markers(
            MagicMock(),
            prior,
            f"PRELOOP_AGENT_SESSION codex {session_id}",
            isolated_publication=False,
        )
    assert record.call_args.args[1:] == (
        prior.id,
        {"agent_type": "codex", "session_id": session_id},
    )


def test_runner_replayed_plain_marker_does_not_discard_native_artifact() -> None:
    _, prior, _ = legacy_binding()
    prior.cli_session = {
        "session_id": str(uuid4()),
        "artifact_reference": {"artifact_id": str(uuid4())},
    }
    with patch.object(binding, "record_cli_session") as record:
        binding.record_runner_handoff_markers(
            MagicMock(),
            prior,
            f"PRELOOP_AGENT_SESSION codex {prior.cli_session['session_id']}",
            isolated_publication=False,
        )
    record.assert_not_called()


@pytest.mark.parametrize("kind", ["pr", "native"])
def test_runner_handoff_write_error_propagates_before_ack(kind: str) -> None:
    _, prior, _ = legacy_binding()
    prior.cli_session = None
    prior.result = None
    db = MagicMock()
    if kind == "pr":
        line = 'PRELOOP_PR_OPENED {"url":"https://github.com/example/repo/pull/42","branch":"fix/42"}'
        method = "bind_publication"
    else:
        line = f"PRELOOP_AGENT_SESSION codex {uuid4()}"
        method = "set_cli_session"
    with (
        patch.object(binding.crud_flow_execution, "get", return_value=prior),
        patch.object(
            binding.crud_flow_execution,
            method,
            side_effect=RuntimeError("database unavailable"),
        ),
    ):
        with pytest.raises(RuntimeError, match="database unavailable"):
            binding.record_runner_handoff_markers(
                db, prior, line, isolated_publication=False
            )
    db.rollback.assert_called_once()


@pytest.mark.parametrize("mismatch", ["thread", "execution"])
def test_native_artifact_marker_cannot_cross_handoff_scope(mismatch: str) -> None:
    import json

    _, prior, _ = legacy_binding()
    prior.cli_session = None
    prior.trigger_event_details["_session_thread_id"] = str(uuid4())
    artifact = {
        "agent_type": "codex",
        "session_id": str(uuid4()),
        "thread_id": prior.trigger_event_details["_session_thread_id"],
        "artifact_reference": {"execution_id": str(prior.id)},
    }
    if mismatch == "thread":
        artifact["thread_id"] = str(uuid4())
    else:
        artifact["artifact_reference"]["execution_id"] = str(uuid4())
    with patch.object(binding, "record_cli_session") as record:
        binding.record_runner_handoff_markers(
            MagicMock(),
            prior,
            "PRELOOP_NATIVE_SESSION_ARTIFACT " + json.dumps(artifact),
            isolated_publication=False,
        )
    record.assert_not_called()

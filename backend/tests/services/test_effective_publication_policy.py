"""Saved policy reporting must not silently migrate flows or attest execution."""

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from preloop.models.schemas.flow import FlowResponse
from preloop.services.verification import describe_effective_publication_policy


@pytest.mark.parametrize(
    "config,mode,blockers",
    [
        (None, "disabled", []),
        ({"enabled": True}, "ungated", []),
        ({"enabled": True, "verification": {"mode": "off"}}, "ungated", []),
        (
            {"enabled": True, "publication_mode": "isolated"},
            "blocked",
            ["verification_gate_required"],
        ),
        (
            {"enabled": True, "verification": "broken"},
            "blocked",
            ["verification_policy_invalid"],
        ),
    ],
)
def test_existing_policy_is_reported_without_mutation(config, mode, blockers) -> None:
    original = deepcopy(config)
    result = describe_effective_publication_policy(config)
    assert result.mode == mode
    assert result.blockers == blockers
    assert config == original


@pytest.mark.parametrize(
    "isolated,image,mode",
    [
        (False, None, "sandbox_gated"),
        (True, None, "blocked"),
        (True, "example.com/verifier:latest", "blocked"),
        (True, "example.com/verifier@sha256:" + "a" * 64, "isolated"),
    ],
)
def test_configured_gate_is_not_a_runtime_attestation(isolated, image, mode) -> None:
    config = {
        "enabled": True,
        "publication_mode": "isolated" if isolated else "legacy",
        # Repository selection may be deferred to the trigger.
        "repositories": [],
        "verification": {
            "mode": "gate",
            "image": image,
            "profile": {
                "profile_id": "repository-checks",
                "always": [
                    {"id": "lint", "command": "ruff check .", "reason": "required"}
                ],
            },
        },
    }
    result = describe_effective_publication_policy(config)
    assert result.mode == mode
    assert result.configured_check_ids == ["lint"]
    assert result.runtime_validation_required == isolated
    assert "trusted" not in result.model_dump()


def test_flow_response_derives_policy_and_ignores_supplied_summary() -> None:
    now = datetime.now(timezone.utc)
    response = FlowResponse(
        id=uuid4(),
        name="Saved flow",
        created_at=now,
        updated_at=now,
        effective_publication_policy={"mode": "isolated", "reason": "forged"},
        git_clone_config={"enabled": True},
    )
    assert response.model_dump()["effective_publication_policy"]["mode"] == "ungated"

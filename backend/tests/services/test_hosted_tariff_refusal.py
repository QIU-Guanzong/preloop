"""A hosted model with no operator tariff must fail once, not six times.

Observed shape: three agent runs in a row each burned five retries against a
hosted model the deployment had simply never priced. The refusal was
503-shaped, so the gateway retried it, the usage row called it
``upstream_error``, and the harness read that as a provider outage worth
resuming. Nothing upstream was ever contacted, and no attempt could have
changed the answer.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from preloop.models import models
from preloop.services.flow_failure_category import derive_failure_category
from preloop.services.hosted_spend_guard import (
    HOSTED_TARIFF_UNCONFIGURED_CODE,
    hosted_tariff_unconfigured_error,
)
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.upstream_errors import (
    ERROR_CLASS_HOSTED_TARIFF_UNCONFIGURED,
    classify_recorded_error,
)


def _model() -> models.AIModel:
    return models.AIModel(
        id=uuid4(),
        name="Gemini 3.8 Flash",
        account_id=None,
        provider_name="google",
        model_identifier="gemini-3.8-flash",
        meta_data={"gateway": {"model_alias": "google/gemini-3.8-flash"}},
    )


def _service() -> OpenAIGatewayService:
    return OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(
            token="test", user=SimpleNamespace(id=uuid4(), account_id=uuid4())
        ),
        upstream_backend=MagicMock(),
    )


def test_refusal_names_the_model_and_the_way_out() -> None:
    error = hosted_tariff_unconfigured_error(
        _model(), reason="Hosted model needs a verified model context bound"
    )
    assert error.status_code == 503
    assert error.code == HOSTED_TARIFF_UNCONFIGURED_CODE
    assert error.error_class == ERROR_CLASS_HOSTED_TARIFF_UNCONFIGURED
    assert error.terminal is True
    assert error.message.startswith(
        "Hosted model google/gemini-3.8-flash has no operator tariff; "
        "use your own provider key or pick another model."
    )
    # The specific gap is for the operator, after the sentence the caller acts on.
    assert "verified model context bound" in error.message


def test_refusal_without_a_resolvable_alias_still_names_something() -> None:
    """The message is built while a request is failing; it may not resolve."""
    error = hosted_tariff_unconfigured_error(object())
    assert "Hosted model unknown has no operator tariff" in error.message


def test_the_gateway_attempts_it_exactly_once() -> None:
    """The retry loop used to spend five attempts on a fact about config."""
    refusal = hosted_tariff_unconfigured_error(_model())
    operation = MagicMock(side_effect=refusal)
    service = _service()
    with (
        patch("preloop.services.openai_gateway._sleep_before_upstream_retry") as sleep,
        patch("preloop.services.openai_gateway.reserve_gateway_5xx_alert") as reserve,
    ):
        with pytest.raises(ModelGatewayAPIError) as raised:
            service._run_with_upstream_retries("openai", operation)
    assert operation.call_count == 1
    sleep.assert_not_called()
    # Re-raised unchanged: the client sees the code, not a mapped 502.
    assert raised.value is refusal
    reserve.assert_not_called()


def test_the_usage_row_calls_it_configuration_not_upstream() -> None:
    message = hosted_tariff_unconfigured_error(_model()).message
    # Recording infers the class from status plus detail when the caller has
    # none, which is the path a plugin-raised refusal takes.
    assert (
        classify_recorded_error(503, message) == ERROR_CLASS_HOSTED_TARIFF_UNCONFIGURED
    )
    assert (
        OpenAIGatewayService._audit_error_type(
            503, message, ERROR_CLASS_HOSTED_TARIFF_UNCONFIGURED
        )
        == ERROR_CLASS_HOSTED_TARIFF_UNCONFIGURED
    )
    # A genuine provider 503 keeps its old audit bucket.
    assert (
        OpenAIGatewayService._audit_error_type(503, "Origin unavailable")
        == "upstream_error"
    )


def test_the_harness_counts_it_as_model_config() -> None:
    message = hosted_tariff_unconfigured_error(_model()).message
    assert (
        derive_failure_category(status="FAILED", error_message=message)
        == "model_config"
    )

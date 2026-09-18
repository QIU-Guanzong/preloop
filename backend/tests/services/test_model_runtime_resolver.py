"""Tests for model runtime resolution."""

from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_ai_model
from preloop.services.model_runtime_resolver import (
    default_model_gateway_url,
    gateway_url_for_api,
    resolve_ai_model_runtime,
)


def test_resolve_ai_model_runtime_for_gateway_enabled_model(db_session: Session):
    """Gateway-enabled models should resolve to gateway fields only."""
    account = crud_account.create(
        db_session,
        obj_in={
            "organization_name": "Gateway Runtime Resolver Org",
            "is_active": True,
        },
    )
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "url": "http://gateway.internal/openai/v1",
                    "model_alias": "openai/gpt-5",
                    "provider_adapter": "preloop",
                }
            },
        },
        account_id=account.id,
    )

    resolved = resolve_ai_model_runtime(ai_model)

    assert resolved.model_gateway_enabled is True
    assert resolved.model_transport_mode == "preloop_gateway"
    assert resolved.model_identifier == "openai/gpt-5"
    assert resolved.model_provider == "preloop"
    assert resolved.model_endpoint == "http://gateway.internal/openai/v1"
    assert resolved.model_api_key is None


def test_gateway_url_for_api_resolves_sibling_gateway_endpoints():
    """Gateway URLs should adapt to the client API shape an agent speaks."""
    assert (
        gateway_url_for_api("https://review.preloop.ai/openai/v1", "gemini")
        == "https://review.preloop.ai/gemini/v1beta"
    )
    assert (
        gateway_url_for_api("https://review.preloop.ai/gemini/v1beta", "openai")
        == "https://review.preloop.ai/openai/v1"
    )
    assert (
        gateway_url_for_api("https://review.preloop.ai/anthropic/v1", "openai")
        == "https://review.preloop.ai/openai/v1"
    )


def test_default_model_gateway_url_uses_k8s_gateway_service(monkeypatch):
    """A split deployment must address the gateway Service, not the API one.

    The API pods run ``PRELOOP_SERVICE_ROLE=api`` and never mount
    ``/openai/v1``, so sending agent traffic to the API Service returns
    ``404 Not Found`` for every model call.
    """
    monkeypatch.delenv("PRELOOP_MODEL_GATEWAY_URL", raising=False)
    monkeypatch.delenv("PRELOOP_MODEL_GATEWAY_URL_K8S", raising=False)
    monkeypatch.delenv("PRELOOP_SERVICE_ROLE", raising=False)
    monkeypatch.setenv(
        "PRELOOP_API_SERVICE_HTTP_ENDPOINT",
        "http://release-preloop-api:80",
    )
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")

    assert default_model_gateway_url() == "http://release-preloop-gateway:80/openai/v1"


def test_default_model_gateway_url_keeps_api_service_for_combined_role(monkeypatch):
    """One process serving both surfaces is reachable at the API Service."""
    monkeypatch.delenv("PRELOOP_MODEL_GATEWAY_URL", raising=False)
    monkeypatch.delenv("PRELOOP_MODEL_GATEWAY_URL_K8S", raising=False)
    monkeypatch.setenv("PRELOOP_SERVICE_ROLE", "all")
    monkeypatch.setenv(
        "PRELOOP_API_SERVICE_HTTP_ENDPOINT",
        "http://release-preloop-api:80",
    )
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")

    assert default_model_gateway_url() == "http://release-preloop-api:80/openai/v1"


def test_default_model_gateway_url_leaves_unconventional_host_alone(monkeypatch):
    """An endpoint outside the chart's naming stays as configured."""
    monkeypatch.delenv("PRELOOP_MODEL_GATEWAY_URL", raising=False)
    monkeypatch.delenv("PRELOOP_MODEL_GATEWAY_URL_K8S", raising=False)
    monkeypatch.delenv("PRELOOP_SERVICE_ROLE", raising=False)
    monkeypatch.setenv(
        "PRELOOP_API_SERVICE_HTTP_ENDPOINT",
        "http://preloop-control-plane:8000",
    )
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")

    assert default_model_gateway_url() == "http://preloop-control-plane:8000/openai/v1"


def test_default_model_gateway_url_prefers_the_k8s_override(monkeypatch):
    """The chart's explicit in-cluster URL wins over any derivation."""
    monkeypatch.delenv("PRELOOP_MODEL_GATEWAY_URL", raising=False)
    monkeypatch.delenv("PRELOOP_SERVICE_ROLE", raising=False)
    monkeypatch.setenv(
        "PRELOOP_MODEL_GATEWAY_URL_K8S",
        "http://release-preloop-gateway:80/openai/v1",
    )
    monkeypatch.setenv(
        "PRELOOP_API_SERVICE_HTTP_ENDPOINT",
        "http://release-preloop-api:80",
    )
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")

    assert default_model_gateway_url() == "http://release-preloop-gateway:80/openai/v1"


def test_default_model_gateway_url_prefers_configured_values(monkeypatch):
    """Explicit gateway URLs should override environment-derived defaults."""
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv(
        "PRELOOP_MODEL_GATEWAY_URL_K8S",
        "http://staging-api:8000/openai/v1",
    )
    monkeypatch.setenv(
        "PRELOOP_API_SERVICE_HTTP_ENDPOINT",
        "http://release-preloop-api:80",
    )
    monkeypatch.setenv(
        "PRELOOP_MODEL_GATEWAY_URL",
        "https://gateway.example.com/openai/v1",
    )

    assert default_model_gateway_url() == "https://gateway.example.com/openai/v1"


def test_resolve_gateway_enabled_model_uses_k8s_default_when_url_unset(
    db_session: Session, monkeypatch
):
    """Gateway metadata without a URL should be safe in Kubernetes pods."""
    monkeypatch.delenv("PRELOOP_MODEL_GATEWAY_URL", raising=False)
    monkeypatch.delenv("PRELOOP_MODEL_GATEWAY_URL_K8S", raising=False)
    monkeypatch.delenv("PRELOOP_SERVICE_ROLE", raising=False)
    monkeypatch.setenv(
        "PRELOOP_API_SERVICE_HTTP_ENDPOINT",
        "http://release-preloop-api:80",
    )
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    account = crud_account.create(
        db_session,
        obj_in={
            "organization_name": "K8S Gateway Runtime Resolver Org",
            "is_active": True,
        },
    )
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway DeepSeek Model",
            "provider_name": "deepseek",
            "model_identifier": "deepseek-v4-pro",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": "deepseek/deepseek-v4-pro",
                    "provider_adapter": "preloop",
                }
            },
        },
        account_id=account.id,
    )

    resolved = resolve_ai_model_runtime(ai_model)

    assert resolved.model_gateway_enabled is True
    assert resolved.model_gateway_url == "http://release-preloop-gateway:80/openai/v1"
    assert resolved.model_endpoint == "http://release-preloop-gateway:80/openai/v1"


def test_resolve_ai_model_runtime_for_direct_provider_model(db_session: Session):
    """Direct-provider models should still resolve a provider API key."""
    account = crud_account.create(
        db_session,
        obj_in={
            "organization_name": "Direct Runtime Resolver Org",
            "is_active": True,
        },
    )
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Direct Model",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_endpoint": "https://api.anthropic.com/v1",
            "api_key": "provider-secret",
        },
        account_id=account.id,
    )

    resolved = resolve_ai_model_runtime(ai_model)

    assert resolved.model_gateway_enabled is False
    assert resolved.model_transport_mode == "direct_provider"
    assert resolved.model_identifier == "claude-sonnet-4-5"
    assert resolved.model_provider == "anthropic"
    assert resolved.model_endpoint == "https://api.anthropic.com/v1"
    assert resolved.model_api_key == "provider-secret"


def test_resolve_ai_model_runtime_for_structured_direct_credentials(
    db_session: Session,
):
    """Structured credentials should flow through the direct runtime resolver."""
    account = crud_account.create(
        db_session,
        obj_in={
            "organization_name": "Structured Runtime Resolver Org",
            "is_active": True,
        },
    )
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Codex Direct Model",
            "provider_name": "openai-codex",
            "model_identifier": "gpt-5.4",
            "api_endpoint": "https://chatgpt.com/backend-api/codex",
            "credential_type": "oauth_openai_codex",
            "credential_payload": {
                "access": "access-token",
                "refresh": "refresh-token",
                "expires": 1893456000000,
                "account_id": "acct-123",
            },
        },
        account_id=account.id,
    )

    resolved = resolve_ai_model_runtime(ai_model)

    assert resolved.model_gateway_enabled is False
    assert resolved.model_transport_mode == "direct_provider"
    assert resolved.model_identifier == "gpt-5.4"
    assert resolved.model_provider == "openai-codex"
    assert resolved.model_api_key is None
    assert resolved.model_auth_type == "oauth_openai_codex"
    assert resolved.model_auth_payload == {
        "type": "oauth_openai_codex",
        "access": "access-token",
        "refresh": "refresh-token",
        "expires": 1893456000000,
        "account_id": "acct-123",
    }

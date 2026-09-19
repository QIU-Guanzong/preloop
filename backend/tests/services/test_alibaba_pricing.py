"""Alibaba tariffs must match the serving region and reported token classes."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from preloop.models import models
from preloop.services.alibaba_price_catalog import (
    ingest_native_models,
    reset_live_state_for_tests,
)
from preloop.services.ai_model_pricing import _catalog_entry
from preloop.services.model_pricing import (
    CostEstimate,
    estimate_ai_model_usage_cost_detailed,
)


@pytest.fixture(autouse=True)
def _reset_alibaba_overlay() -> None:
    reset_live_state_for_tests()
    yield
    reset_live_state_for_tests()


def _model(model: str = "qwen3.8-max", **kwargs: str) -> models.AIModel:
    return models.AIModel(
        provider_name=kwargs.get("provider", "qwen"),
        model_identifier=model,
        api_endpoint=kwargs.get(
            "endpoint", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        ),
    )


def _estimate(
    model: models.AIModel, usage: dict | None = None, **kwargs
) -> CostEstimate:
    return estimate_ai_model_usage_cost_detailed(
        model,
        prompt_tokens=10000,
        completion_tokens=1000,
        total_tokens=11000,
        usage_details=usage,
        **kwargs,
    )


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("qwen3.8-max", 0.026),
        ("deepseek-v4-pro", 0.0288),
        ("deepseek-v4-flash", 0.0024),
        ("glm-5.2", 0.0184),
        ("glm-5.3", 0.0184),
        ("kimi-k2.7-code", 0.0135),
        ("kimi-k3", 0.045),
        ("qwen3.8-flash", 0.00197),
    ],
)
def test_singapore_headline_list_costs(model: str, expected: float) -> None:
    result = _estimate(_model(model))
    assert result.source == "catalog"
    assert result.cost == pytest.approx(expected)


def test_time_banded_deepseek_uses_utc8_night_window() -> None:
    """Singapore International busy/idle rates follow 22:00-08:00 UTC+8."""
    model = _model("deepseek-v4.1-flash")
    # 04:00 UTC is 12:00 UTC+8 (daytime / busy).
    busy = _estimate(
        model, observed_at=datetime(2026, 9, 19, 4, 0, tzinfo=timezone.utc)
    )
    # 16:00 UTC is 00:00 UTC+8 (night / idle).
    idle = _estimate(
        model, observed_at=datetime(2026, 9, 19, 16, 0, tzinfo=timezone.utc)
    )
    assert busy.source == "catalog"
    assert idle.source == "catalog"
    assert busy.cost == pytest.approx(0.0042)
    assert idle.cost == pytest.approx(0.0021)
    flash_0731 = _estimate(
        _model("deepseek-v4-flash-0731"),
        observed_at=datetime(2026, 9, 19, 4, 0, tzinfo=timezone.utc),
    )
    assert flash_0731.cost == pytest.approx(0.00572)
    cached = _estimate(
        model,
        {
            "_preloop_cache_mode": "implicit",
            "prompt_tokens_details": {"cached_tokens": 5000},
        },
        observed_at=datetime(2026, 9, 19, 4, 0, tzinfo=timezone.utc),
    )
    assert cached.cost == pytest.approx(0.00285)
    unpriced_cache = _estimate(
        _model("deepseek-v4-flash-0731"),
        {
            "_preloop_cache_mode": "implicit",
            "prompt_tokens_details": {"cached_tokens": 5000},
        },
        observed_at=datetime(2026, 9, 19, 4, 0, tzinfo=timezone.utc),
    )
    assert unpriced_cache.cost is None
    assert _catalog_entry(model) is not None


def test_alibaba_idle_hours_follow_utc8_night_window() -> None:
    from preloop.services.alibaba_pricing import is_alibaba_idle_hour

    # 22:00 UTC+8 is 14:00 UTC; 08:00 UTC+8 is 00:00 UTC.
    assert is_alibaba_idle_hour(datetime(2026, 9, 19, 14, 0, tzinfo=timezone.utc))
    assert is_alibaba_idle_hour(datetime(2026, 9, 18, 23, 59, tzinfo=timezone.utc))
    assert not is_alibaba_idle_hour(datetime(2026, 9, 19, 0, 0, tzinfo=timezone.utc))
    assert not is_alibaba_idle_hour(datetime(2026, 9, 19, 13, 59, tzinfo=timezone.utc))


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
        "https://tenant.us-east-1.maas.aliyuncs.com/compatible-mode/v1",
    ],
)
def test_unverified_region_never_uses_international_or_native_rate(
    endpoint: str,
) -> None:
    model = _model("deepseek-v4-pro", endpoint=endpoint)
    assert _estimate(model).cost is None
    assert _estimate(model).source == "unpriced"
    assert _catalog_entry(model) is None


def test_workspace_and_custom_provider_share_the_exact_serving_tariff() -> None:
    model = _model(
        "deepseek-v4-pro",
        provider="custom",
        endpoint="https://tenant.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
    )
    with patch("litellm.cost_per_token", side_effect=AssertionError("wrong provider")):
        assert _estimate(model).cost == pytest.approx(0.0288)
    key, entry = _catalog_entry(model)
    assert "singapore" in key
    assert entry["input_cost_per_token"] == pytest.approx(2.4e-6)


def test_unknown_snapshot_and_gateway_alias_do_not_borrow_a_price() -> None:
    model = _model("deepseek-v4-pro-unknown")
    model.meta_data = {"gateway": {"model_alias": "deepseek-v4-pro"}}
    assert _estimate(model).cost is None
    assert _catalog_entry(model) is None


@pytest.mark.parametrize("mode", ["implicit", "explicit"])
def test_native_cache_observations_without_currency_are_not_prices(mode: str) -> None:
    result = _estimate(
        _model(),
        {
            "_preloop_cache_mode": mode,
            "prompt_tokens_details": {"cached_tokens": 5000},
            "completion_tokens_details": {"reasoning_tokens": 900},
        },
    )
    assert result.cost is None
    assert result.source == "unpriced"


def test_old_cached_usage_without_mode_is_unknown() -> None:
    assert (
        _estimate(_model(), {"prompt_tokens_details": {"cached_tokens": 5000}}).cost
        is None
    )


def test_nested_cache_creation_never_uses_an_unverified_tariff() -> None:
    usage = {
        "_preloop_cache_mode": "explicit",
        "prompt_tokens_details": {
            "cached_tokens": 4000,
            "cache_creation_input_tokens": 1000,
        },
        "completion_tokens_details": {"reasoning_tokens": 900},
    }
    assert _estimate(_model(), usage).cost is None


def test_official_glm_cache_ratio_and_reasoning_are_not_double_counted() -> None:
    usage = {
        "_preloop_cache_mode": "implicit",
        "prompt_tokens_details": {"cached_tokens": 5000},
        "completion_tokens_details": {"reasoning_tokens": 900},
    }
    # 5k standard at1.4 +5k cached at25% +1k total output at4.4 per1M.
    assert _estimate(_model("glm-5.2"), usage).cost == pytest.approx(0.01315)


def test_openrouter_route_keeps_its_authoritative_provider_accounting() -> None:
    result = _estimate(
        _model(endpoint="https://openrouter.ai/api/v1"), {"cost": 0.0123}
    )
    assert result.cost == 0.0123
    assert result.source == "provider"


def test_whitespace_on_endpoint_does_not_change_tariff() -> None:
    result = _estimate(
        _model(endpoint=" https://dashscope-intl.aliyuncs.com/compatible-mode/v1 ")
    )
    assert result.cost == pytest.approx(0.026)


def test_unknown_cache_tariff_stays_unpriced() -> None:
    assert (
        _estimate(
            _model("deepseek-v4-pro"),
            {
                "_preloop_cache_mode": "implicit",
                "prompt_tokens_details": {"cached_tokens": 1000},
            },
        ).cost
        is None
    )


def test_operator_override_remains_authoritative() -> None:
    result = _estimate(
        _model("unknown"),
        pricing_override={
            "input_price_per_1k": 0.01,
            "output_price_per_1k": 0.02,
            "cache_creation_input_price_per_1k": 0.02,
        },
        usage={"prompt_tokens_details": {"cache_creation_input_tokens": 1000}},
    )
    assert result.source == "override"
    assert result.cost == pytest.approx(0.13)


@pytest.mark.parametrize("currency", [None, "CNY", "USD"])
def test_undocumented_provider_money_cannot_bypass_tariff(currency: str | None) -> None:
    result = _estimate(_model(), {"cost": 0.0123, "currency": currency})
    assert result.cost == pytest.approx(0.026)
    assert result.source == "catalog"


def test_qwen_context_limit_does_not_fall_back_to_a_lower_tier() -> None:
    result = estimate_ai_model_usage_cost_detailed(
        _model(),
        prompt_tokens=1_000_001,
        completion_tokens=1000,
        total_tokens=1_001_001,
    )
    assert result.cost is None


def test_impossible_cache_counts_do_not_produce_negative_cost() -> None:
    assert (
        _estimate(
            _model(),
            {
                "_preloop_cache_mode": "implicit",
                "prompt_tokens_details": {"cached_tokens": 11000},
            },
        ).cost
        is None
    )


def test_snapshot_with_gateway_alias_prices_observed_uncached_usage() -> None:
    """A dated upstream ID retains its tariff despite the prefixed client alias."""
    model = _model(
        "qwen3.8-max-0902",
        endpoint="https://tenant.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
    )
    model.meta_data = {"gateway": {"model_alias": "qwen/qwen3.8-max-0902"}}
    result = estimate_ai_model_usage_cost_detailed(
        model,
        prompt_tokens=72,
        completion_tokens=69,
        total_tokens=141,
        usage_details={
            "prompt_tokens": 72,
            "completion_tokens": 69,
            "total_tokens": 141,
            "prompt_tokens_details": {
                "text_tokens": 72,
                "audio_tokens": None,
                "cached_tokens": 0,
            },
            "completion_tokens_details": {
                "text_tokens": 69,
                "audio_tokens": None,
                "reasoning_tokens": 59,
            },
        },
    )
    # Reasoning is included in the 69 output tokens, not added again.
    assert result.cost == pytest.approx(0.000558)
    assert result.source == "catalog"


def test_optional_rate_rejects_nan() -> None:
    from preloop.services.alibaba_pricing import _optional_rate

    assert _optional_rate(float("nan")) is None
    assert _optional_rate("nan") is None
    assert _optional_rate(0.25) == 0.25


def test_seed_covers_current_singapore_chat_skus() -> None:
    from preloop.services.alibaba_pricing import _SEED

    assert "qwen3.8-flash" in _SEED
    assert "qwen3.5-flash" in _SEED
    assert "qwen-plus" in _SEED
    assert "deepseek-v4.1-flash" in _SEED
    assert "glm-5.3" in _SEED
    assert len(_SEED) >= 80


def test_verified_standard_cache_ratios_cover_supported_qwen() -> None:
    usage = {
        "_preloop_cache_mode": "implicit",
        "prompt_tokens_details": {"cached_tokens": 5000},
    }
    assert _estimate(_model("qwen3.7-flash"), usage).cost == pytest.approx(0.00031)


@pytest.mark.parametrize(
    ("prompt", "rate"),
    [(32000, 0.03), (32001, 0.1), (256000, 0.1), (256001, 0.2), (1000000, 0.2)],
)
def test_verified_flash_all_context_tiers(prompt: int, rate: float) -> None:
    from preloop.services.alibaba_pricing import estimate

    assert estimate(
        _model("qwen3.7-flash"),
        prompt_tokens=prompt,
        completion_tokens=0,
        usage_details=None,
    ) == pytest.approx(round(prompt * rate / 1_000_000, 6))


@pytest.mark.parametrize(
    ("mode", "created", "expected"),
    [
        ("implicit", 0, 0.001538),
        ("explicit", 1000, 0.001588),
    ],
)
def test_operator_verified_flash_workspace_cache_tariffs(
    mode: str,
    created: int,
    expected: float,
) -> None:
    from preloop.services.alibaba_pricing import pricing_failure_reason

    model = _model(
        "qwen3.8-flash",
        endpoint="https://workspace.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
    )
    usage = {
        "_preloop_cache_mode": mode,
        "prompt_tokens_details": {
            "cached_tokens": 48000,
            "cache_creation_input_tokens": created,
        },
    }
    result = estimate_ai_model_usage_cost_detailed(
        model,
        prompt_tokens=50000,
        completion_tokens=1000,
        total_tokens=51000,
        usage_details=usage,
    )
    assert result.source == "catalog"
    assert result.cost == pytest.approx(expected)
    assert (
        pricing_failure_reason(model, prompt_tokens=50000, usage_details=usage) is None
    )


def test_console_seed_cache_quote_does_not_invent_historical_applicability() -> None:
    from datetime import datetime, timezone
    from preloop.services.alibaba_pricing import estimate

    model = _model("qwen3.8-flash")
    before = datetime(2026, 9, 15, 16, 26, 49, tzinfo=timezone.utc)
    confirmed = datetime(2026, 9, 15, 16, 26, 50, tzinfo=timezone.utc)
    usage = {
        "_preloop_cache_mode": "implicit",
        "prompt_tokens_details": {"cached_tokens": 48000},
    }
    kwargs = {"prompt_tokens": 50000, "completion_tokens": 1000, "usage_details": usage}
    assert estimate(model, **kwargs, observed_at=before) is None
    assert estimate(model, **kwargs, observed_at=confirmed) == pytest.approx(0.001538)
    assert estimate(
        model,
        prompt_tokens=50000,
        completion_tokens=1000,
        usage_details=None,
        observed_at=before,
    ) == pytest.approx(0.00797)


def test_reviewed_flash_cache_rate_prices_workspace_without_native_credentials() -> (
    None
):
    from datetime import datetime, timedelta, timezone
    from preloop.services.alibaba_price_catalog import install_reviewed_catalogs
    from preloop.services.alibaba_pricing import Tariff

    now = datetime.now(timezone.utc)
    # Synthetic reviewed tariff tests delivery; not an asserted provider rate.
    install_reviewed_catalogs(
        {
            "singapore-international": {
                "qwen3.8-flash": Tariff(input=1, output=2, implicit_read=0.1)
            }
        },
        verified_at=now,
        expires_at=now + timedelta(days=1),
        revision="fixture",
    )
    model = _model(
        "qwen3.8-flash",
        endpoint="https://tenant.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
    )
    result = _estimate(
        model,
        {
            "_preloop_cache_mode": "implicit",
            "prompt_tokens_details": {"cached_tokens": 5000},
        },
    )
    assert result.cost == pytest.approx(0.0075)
    assert _catalog_entry(model)[0] == "alibaba/reviewed-catalog/qwen3.8-flash"


def test_live_native_overlay_prices_cache_on_usd_site() -> None:
    ingest_native_models(
        [
            {
                "model": "qwen3.8-max",
                "prices": [
                    {
                        "range_name": "Default",
                        "prices": [
                            {
                                "type": "input_token",
                                "price": "2",
                                "price_unit": "Per 1M tokens",
                            },
                            {
                                "type": "output_token",
                                "price": "6",
                                "price_unit": "Per 1M tokens",
                            },
                            {
                                "type": "input_token_cache",
                                "price": "0.25",
                                "price_unit": "Per 1M tokens",
                            },
                        ],
                    }
                ],
            }
        ],
        region="singapore-international",
    )
    result = _estimate(
        _model(),
        {
            "_preloop_cache_mode": "implicit",
            "prompt_tokens_details": {"cached_tokens": 5000},
        },
    )
    # 5k standard at $2 + 5k cached at $0.25 + 1k output at $6 per 1M.
    assert result.source == "catalog"
    assert result.cost == pytest.approx(0.01725)


def test_whole_request_tier_is_selected_not_the_lowest() -> None:
    ingest_native_models(
        [
            {
                "model": "tiered-chat",
                "prices": [
                    {
                        "range_name": "0<Token<=32k",
                        "prices": [
                            {
                                "type": "input_token",
                                "price": "1",
                                "price_unit": "Per 1M tokens",
                            },
                            {
                                "type": "output_token",
                                "price": "2",
                                "price_unit": "Per 1M tokens",
                            },
                        ],
                    },
                    {
                        "range_name": "32k<Input<=256k",
                        "prices": [
                            {
                                "type": "input_token",
                                "price": "4",
                                "price_unit": "Per 1M tokens",
                            },
                            {
                                "type": "output_token",
                                "price": "8",
                                "price_unit": "Per 1M tokens",
                            },
                        ],
                    },
                ],
            }
        ],
        region="singapore-international",
    )
    model = _model("tiered-chat")
    low = estimate_ai_model_usage_cost_detailed(
        model, prompt_tokens=10_000, completion_tokens=0, total_tokens=10_000
    )
    mid = estimate_ai_model_usage_cost_detailed(
        model, prompt_tokens=100_000, completion_tokens=0, total_tokens=100_000
    )
    over = estimate_ai_model_usage_cost_detailed(
        model, prompt_tokens=300_000, completion_tokens=0, total_tokens=300_000
    )
    assert low.cost == pytest.approx(0.01)
    assert mid.cost == pytest.approx(0.4)
    assert over.cost is None


def test_reviewed_effective_dates_are_per_model_and_block_seed_fallback() -> None:
    from datetime import datetime, timedelta, timezone
    from preloop.services.alibaba_price_catalog import install_reviewed_catalogs
    from preloop.services.alibaba_pricing import Tariff, estimate

    now = datetime.now(timezone.utc)
    earlier = now - timedelta(days=2)
    later = now - timedelta(days=1)
    install_reviewed_catalogs(
        {
            "singapore-international": {
                "qwen3.8-flash": Tariff(input=1, output=2),
                "qwen3.8-max": Tariff(input=3, output=4),
            }
        },
        verified_at=now,
        expires_at=now + timedelta(days=1),
        revision="dated",
        effective_from={
            "singapore-international": {
                "qwen3.8-flash": earlier,
                "qwen3.8-max": later,
            }
        },
    )
    kwargs = {
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "usage_details": None,
        "observed_at": earlier,
    }
    assert estimate(_model("qwen3.8-flash"), **kwargs) == pytest.approx(0.0012)
    assert estimate(_model("qwen3.8-max"), **kwargs) is None
    kwargs["observed_at"] = later
    assert estimate(_model("qwen3.8-max"), **kwargs) == pytest.approx(0.0034)

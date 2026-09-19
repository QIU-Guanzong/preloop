"""Reviewed publication tooling must not invent evidence or arm duplicate flows."""

import copy
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def dump() -> dict:
    return {
        "_meta": {
            "currency": "USD",
            "region": "singapore",
            "service_site": "international",
            "complete": True,
            "source_url": "https://dashscope-intl.aliyuncs.com/api/v1/models",
            "retrieved_at": (
                datetime.now(timezone.utc) - timedelta(days=1)
            ).isoformat(),
        },
        "output": {
            "total": 1,
            "models": [
                {
                    "model": "example-chat",
                    "prices": [
                        {
                            "range_name": "Default",
                            "prices": [
                                {
                                    "type": "input_token",
                                    "price": "0.2",
                                    "price_unit": "per 1m tokens",
                                },
                                {
                                    "type": "output_token",
                                    "price": "0.8",
                                    "price_unit": "per 1m tokens",
                                },
                            ],
                        }
                    ],
                }
            ],
        },
    }


def test_native_seed_preserves_original_evidence_and_absent_cache(dump: dict) -> None:
    builder = load_script("update_alibaba_prices")
    seed = builder.build_seed(dump)
    assert seed["_meta"]["retrieved_at"] == dump["_meta"]["retrieved_at"]
    assert seed["models"]["example-chat"]["tiers"] == [{"input": 0.2, "output": 0.8}]


def test_native_seed_keeps_idle_and_busy_bands(dump: dict) -> None:
    dump["output"]["models"][0]["model"] = "deepseek-v4.1-flash"
    dump["output"]["models"][0]["prices"][0]["prices"] = [
        {
            "type": "input_token",
            "price": "0.3",
            "price_unit": "Per 1M tokens",
            "time_band": "busy",
        },
        {
            "type": "output_token",
            "price": "1.2",
            "price_unit": "Per 1M tokens",
            "time_band": "busy",
        },
        {
            "type": "input_token",
            "price": "0.15",
            "price_unit": "Per 1M tokens",
            "time_band": "idle",
        },
        {
            "type": "output_token",
            "price": "0.6",
            "price_unit": "Per 1M tokens",
            "time_band": "idle",
        },
    ]
    seed = load_script("update_alibaba_prices").build_seed(dump)
    bands = seed["models"]["deepseek-v4.1-flash"]["time_bands"]
    assert bands["busy"]["tiers"] == [{"input": 0.3, "output": 1.2}]
    assert bands["idle"]["tiers"] == [{"input": 0.15, "output": 0.6}]


@pytest.mark.parametrize(
    "fault", ["partial", "wrong_region", "stale", "duplicate", "empty", "raw", "future"]
)
def test_native_seed_rejects_unsafe_source_before_writing(
    dump: dict, fault: str
) -> None:
    if fault == "partial":
        dump["output"]["total"] = 2
    elif fault == "wrong_region":
        dump["_meta"]["currency"] = "CNY"
    elif fault == "stale":
        dump["_meta"]["retrieved_at"] = (
            datetime.now(timezone.utc) - timedelta(days=20)
        ).isoformat()
    elif fault == "future":
        dump["_meta"]["retrieved_at"] = (
            datetime.now(timezone.utc) + timedelta(days=1)
        ).isoformat()
    elif fault == "duplicate":
        dump["output"]["models"] *= 2
        dump["output"]["total"] = 2
    elif fault == "empty":
        dump["output"] = {"models": [], "total": 0}
    else:
        dump = dump["output"]["models"]
    with pytest.raises(ValueError):
        load_script("update_alibaba_prices").build_seed(dump)


def test_regional_feed_copies_dedicated_seed(dump: dict) -> None:
    seed = load_script("update_alibaba_prices").build_seed(dump)
    now = datetime.now(timezone.utc)
    key = "alibaba/singapore-international/example-chat"
    manifest = {
        "schema_version": 1,
        "currency": "USD",
        "revision": "test-1",
        "published_at": now.isoformat(),
        "expires_at": (now + timedelta(days=7)).isoformat(),
        "models": {
            key: {
                "policy": "alibaba_regional_tokens",
                "source_url": "https://example.com/pricing",
                "verified_at": dump["_meta"]["retrieved_at"],
                "effective_from": dump["_meta"]["retrieved_at"],
            }
        },
    }
    feed = load_script("build_reviewed_model_prices").build_feed(
        {}, manifest, {"singapore-international": seed}
    )
    assert feed["models"][key]["alibaba_policy"]["tiers"][0]["input"] == 0.2
    seed["_meta"]["currency"] = "CNY"
    with pytest.raises(ValueError, match="currency or region"):
        load_script("build_reviewed_model_prices").build_feed(
            {}, manifest, {"singapore-international": seed}
        )


def test_regional_feed_round_trips_time_bands(dump: dict) -> None:
    dump["output"]["models"][0]["model"] = "deepseek-v4.1-flash"
    dump["output"]["models"][0]["prices"][0]["prices"] = [
        {
            "type": "input_token",
            "price": "0.3",
            "price_unit": "Per 1M tokens",
            "time_band": "busy",
        },
        {
            "type": "output_token",
            "price": "1.2",
            "price_unit": "Per 1M tokens",
            "time_band": "busy",
        },
        {
            "type": "input_token",
            "price": "0.15",
            "price_unit": "Per 1M tokens",
            "time_band": "idle",
        },
        {
            "type": "output_token",
            "price": "0.6",
            "price_unit": "Per 1M tokens",
            "time_band": "idle",
        },
    ]
    seed = load_script("update_alibaba_prices").build_seed(dump)
    now = datetime.now(timezone.utc)
    key = "alibaba/singapore-international/deepseek-v4.1-flash"
    manifest = {
        "schema_version": 1,
        "currency": "USD",
        "revision": "test-1",
        "published_at": now.isoformat(),
        "expires_at": (now + timedelta(days=7)).isoformat(),
        "models": {
            key: {
                "policy": "alibaba_regional_tokens",
                "source_url": "https://example.com/pricing",
                "verified_at": dump["_meta"]["retrieved_at"],
                "effective_from": dump["_meta"]["retrieved_at"],
            }
        },
    }
    feed = load_script("build_reviewed_model_prices").build_feed(
        {}, manifest, {"singapore-international": seed}
    )
    policy = feed["models"][key]["alibaba_policy"]
    assert policy.get("tiers") is None
    assert policy["time_bands"]["busy"]["tiers"][0]["input"] == 0.3
    assert policy["time_bands"]["idle"]["tiers"][0]["input"] == 0.15


def test_weekly_installer_renders_valid_bound_schedule_and_is_idempotent() -> None:
    installer = load_script("install_model_price_review")
    payload = installer.render_flow(
        ai_model_id="00000000-0000-0000-0000-000000000001",
        tracker_id="00000000-0000-0000-0000-000000000002",
        project_id="00000000-0000-0000-0000-000000000003",
        repository_url="https://github.com/example/project.git",
        enable=True,
    )
    assert payload["schedule_config"]["expr"] == "0 6 * * 1"
    assert payload["trigger_event_source"] == "schedule"
    assert payload["is_enabled"] is True
    assert "REPLACE_" not in str(payload)
    client = MagicMock()
    existing = {**copy.deepcopy(payload), "id": "existing-id"}
    client.get.return_value.json.return_value = [existing]
    assert installer.install_flow(client, payload) == existing
    client.post.assert_not_called()
    client.put.assert_not_called()
    client.get.return_value.json.return_value = [existing, existing]
    with pytest.raises(ValueError, match="Duplicate"):
        installer.install_flow(client, payload)


def test_weekly_installer_does_not_overwrite_unmanaged_flow() -> None:
    installer = load_script("install_model_price_review")
    client = MagicMock()
    client.get.return_value.json.return_value = [
        {"id": "other", "name": "weekly", "description": "user flow"}
    ]
    with pytest.raises(ValueError, match="unmanaged"):
        installer.install_flow(
            client, {"name": "weekly", "description": "Managed marker"}
        )
    client.put.assert_not_called()

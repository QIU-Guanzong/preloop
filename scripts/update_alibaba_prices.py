#!/usr/bin/env python
"""Regenerate the Alibaba Singapore International seed from a native catalog dump.

The runtime overlay fetches native GET /api/v1/models with the model's key.
This script is for the checked-in seed used before that refresh, and for the
weekly review when a native dump is attached as evidence.

    python scripts/update_alibaba_prices.py --from-native dump.json

The dump combines all pages under ``output.models`` and ``output.total``, plus
``_meta`` with complete=true, Singapore/USD region and original UTC retrieval
time/source URL. Raw pages, stale evidence and wrong regions are rejected.
No API keys are read.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from preloop.services.alibaba_price_catalog import parse_native_model  # noqa: E402

SEED_PATH = (
    ROOT
    / "backend"
    / "preloop"
    / "services"
    / "data"
    / "alibaba_international_prices.json"
)


def _models_from_payload(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Require a complete regional dump with original first-party provenance."""
    if not isinstance(payload, dict) or not isinstance(payload.get("_meta"), dict):
        raise ValueError(
            "Native dump needs _meta evidence; raw arrays/pages are unsafe"
        )
    meta = payload["_meta"]
    if (meta.get("currency"), meta.get("service_site"), meta.get("region")) != (
        "USD",
        "international",
        "singapore",
    ):
        raise ValueError("Native dump must establish Singapore International USD")
    source = meta.get("source_url", "")
    if source != "https://dashscope-intl.aliyuncs.com/api/v1/models":
        raise ValueError("Native dump source must be the Singapore native catalog")
    retrieved = datetime.fromisoformat(
        meta.get("retrieved_at", "").replace("Z", "+00:00")
    )
    now = datetime.now(timezone.utc)
    if (
        retrieved.tzinfo is None
        or not 0 <= (now - retrieved).total_seconds() <= 14 * 86400
    ):
        raise ValueError(
            "Native dump requires original timezone-aware evidence within 14 days"
        )
    output = payload.get("output", payload)
    if not isinstance(output, dict):
        raise ValueError("Native dump output must be an object")
    rows = output.get("models")
    total = output.get("total")
    if (
        meta.get("complete") is not True
        or not isinstance(rows, list)
        or type(total) is not int
        or total != len(rows)
    ):
        raise ValueError("Native dump must contain all pages and an exact total")
    identifiers = [row.get("model") for row in rows if isinstance(row, dict)]
    if (
        len(identifiers) != len(rows)
        or any(not isinstance(key, str) or not key.strip() for key in identifiers)
        or len({key.strip() for key in identifiers}) != len(rows)
    ):
        raise ValueError("Native dump has invalid or duplicate model identifiers")
    return rows, meta


def _tier_payload(tier: Any) -> dict[str, Any]:
    return {
        key: value
        for key, value in {
            "max_input": tier.max_input,
            "input": tier.input,
            "output": tier.output,
            "implicit_read": tier.implicit_read,
            "explicit_read": tier.explicit_read,
            "creation": tier.creation,
        }.items()
        if value is not None
    }


def _band_payload(tariff: Any) -> dict[str, Any]:
    tiers = list(tariff.tiers) if tariff.tiers else [tariff]
    return {"tiers": [_tier_payload(tier) for tier in tiers]}


def _seed_model_entry(tariff: Any) -> dict[str, Any]:
    bands = tariff.time_bands
    if bands is not None:
        return {
            "time_bands": {
                "idle": _band_payload(bands.idle),
                "busy": _band_payload(bands.busy),
            }
        }
    return _band_payload(tariff)


def build_seed(payload: Any) -> dict[str, Any]:
    """Convert a verified complete dump without changing its retrieval date."""
    rows, meta = _models_from_payload(payload)
    models: dict[str, dict[str, Any]] = {}
    for entry in rows:
        ident = entry["model"].strip()
        tariff = parse_native_model(entry)
        if tariff is None:
            continue
        models[ident] = _seed_model_entry(tariff)
    if not models:
        raise ValueError("Native dump contains no supported USD token tariffs")
    return {
        "_meta": {
            **meta,
            "model_count": len(models),
            "unsupported_models": sorted(
                row["model"].strip()
                for row in rows
                if row["model"].strip() not in models
            ),
            "note": "Verified Singapore International token tariffs. Estimates, not invoices.",
        },
        "models": {key: models[key] for key in sorted(models)},
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-native", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=SEED_PATH)
    args = parser.parse_args()
    seed = build_seed(json.loads(args.from_native.read_text()))
    args.output.write_text(json.dumps(seed, indent=2) + "\n")
    print(f"wrote {len(seed['models'])} models to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

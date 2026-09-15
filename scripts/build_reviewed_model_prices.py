#!/usr/bin/env python
"""Build a runtime price feed from a reviewed catalog and evidence manifest.

Run from the repository root with PYTHONPATH=backend. The manifest has the
ReviewedPriceFeed shape except that each model's rates and native policy
are omitted: those are copied from the catalog, avoiding duplicate rate tables.
Only the explicitly reviewed model keys are exported. This does not fetch,
publish, or activate prices.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from preloop.services.reviewed_model_price_refresh import PRICE_FIELDS, validate_feed


def build_feed(
    catalog: dict[str, Any],
    manifest: dict[str, Any],
    alibaba_catalogs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Copy reviewed rates/policies, requiring per-model provider evidence."""
    payload = {**manifest, "models": {}}
    for model, evidence in manifest["models"].items():
        if any(
            field in evidence
            for field in (
                "prices",
                "price_policy",
                "alibaba_policy",
                "price_policy_history",
            )
        ):
            raise ValueError(
                "Manifest prices/policy must come from the reviewed catalog"
            )
        if evidence["policy"] == "alibaba_regional_tokens":
            prefix, region, identifier = model.split("/", 2)
            if (
                prefix != "alibaba"
                or not alibaba_catalogs
                or region not in alibaba_catalogs
            ):
                raise ValueError("Alibaba feed needs an explicit regional seed")
            seed = alibaba_catalogs[region]
            meta = seed.get("_meta", {})
            expected_region = (
                "singapore" if region == "singapore-international" else region
            )
            if meta.get("currency") != "USD" or meta.get("region") != expected_region:
                raise ValueError("Alibaba seed currency or region mismatch")
            entry = seed["models"][identifier]
            # Copy shared cache fields into each tier without inventing absent rates.
            tiers = [
                {
                    **{
                        key: entry[key]
                        for key in ("implicit_read", "explicit_read", "creation")
                        if key in entry
                    },
                    **tier,
                }
                for tier in entry["tiers"]
            ]
            payload["models"][model] = {
                **evidence,
                "alibaba_policy": {
                    "region": region,
                    "currency": "USD",
                    "model_identifier": identifier,
                    "tiers": tiers,
                },
            }
            continue
        entry = catalog[model]
        if evidence["policy"] == "deepseek_utc_bands":
            payload["models"][model] = {
                **evidence,
                "price_policy": entry["preloop_price_policy"],
                "price_policy_history": entry.get("preloop_price_policy_history", []),
            }
            continue
        if any("cost" in field and field not in PRICE_FIELDS for field in entry):
            raise ValueError(f"Model {model} has an unsupported price policy")
        payload["models"][model] = {
            **evidence,
            "prices": {key: entry[key] for key in PRICE_FIELDS if key in entry},
        }
    return validate_feed(payload).model_dump(mode="json", exclude_none=True)


def main() -> int:
    """Validate a reviewed artifact before writing it to the requested path."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--alibaba-catalog",
        action="append",
        default=[],
        metavar="REGION=PATH",
        help="Reviewed USD regional seed; repeat for multiple regions",
    )
    args = parser.parse_args()
    alibaba_catalogs = {}
    for source in args.alibaba_catalog:
        region, path = source.split("=", 1)
        if region in alibaba_catalogs:
            parser.error("Duplicate Alibaba region")
        alibaba_catalogs[region] = json.loads(Path(path).read_text())
    payload = build_feed(
        json.loads(args.catalog.read_text()),
        json.loads(args.manifest.read_text()),
        alibaba_catalogs,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"Validated reviewed feed: {len(payload['models'])} model(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

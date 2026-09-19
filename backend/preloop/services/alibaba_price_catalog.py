"""Live Alibaba Model Studio native-catalog prices, cached in-process.

The documented native ``GET /api/v1/models`` response includes per-SKU
tariffs. Chat completions do not. This module is the keep-current path:
Fetch Models, Fetch price, and unpriced-row lookup refresh the overlay.
Estimates remain list prices, never invoices.

Currency is asserted from the serving region (Singapore International and
US workspace native catalogs are USD). Beijing is not ingested into USD
accounting. Time-banded token rows become idle/busy tariffs using Model
Studio night hours (22:00-08:00 UTC+8). Image and other non-token units
stay unpriced.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlparse

import httpx

from preloop.models import models
from preloop.services.alibaba_pricing import (
    Tariff,
    TimeBands,
    _host,
    is_alibaba,
)

logger = logging.getLogger(__name__)

SINGAPORE_NATIVE_URL = "https://dashscope-intl.aliyuncs.com/api/v1/models"
NATIVE_TIMEOUT_SECONDS = 15.0
_PAGE_SIZE = 100
_MAX_PAGES = 10

_TOKEN_UNITS = {
    "per 1m tokens",
    "per million tokens",
    "per 1 million tokens",
}

_lock = threading.Lock()
# region -> {model_id: Tariff}
_live: dict[str, dict[str, Tariff]] = {}
_live_dates: dict[tuple[str, str], datetime] = {}
_reviewed: dict[str, dict[str, Tariff]] = {}
_reviewed_verified_at: datetime | None = None
_reviewed_expires_at: datetime | None = None
_reviewed_revision: str | None = None
_reviewed_effective: dict[str, dict[str, datetime]] = {}
NATIVE_TTL_SECONDS = 24 * 60 * 60
USD_REGIONS = frozenset({"singapore-international", "united-states"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def validate_reviewed_catalogs(
    catalogs: dict[str, dict[str, Tariff]],
    *,
    verified_at: datetime,
    expires_at: datetime,
    revision: str,
    effective_from: dict[str, dict[str, datetime]] | None = None,
) -> None:
    """Validate an entire reviewed USD catalog before any store mutation."""
    if (
        verified_at.tzinfo is None
        or expires_at.tzinfo is None
        or expires_at <= verified_at
        or not revision.strip()
    ):
        raise ValueError("Reviewed Alibaba catalog needs dated provenance")
    for region, entries in catalogs.items():
        if region not in USD_REGIONS:
            raise ValueError("Unsupported Alibaba USD region")
        for ident, tariff in entries.items():
            effective = (effective_from or {}).get(region, {}).get(ident, verified_at)
            if effective.tzinfo is None:
                raise ValueError("Alibaba effective dates must be timezone-aware")
            if not ident.strip() or not isinstance(tariff, Tariff):
                raise ValueError("Invalid Alibaba model tariff")
            for tier in (tariff, *tariff.tiers):
                values = (
                    tier.input,
                    tier.output,
                    tier.implicit_read,
                    tier.explicit_read,
                    tier.creation,
                )
                if any(
                    v is not None and (not math.isfinite(v) or v < 0) for v in values
                ):
                    raise ValueError("Invalid Alibaba token price")
                if tier.max_input is not None and tier.max_input <= 0:
                    raise ValueError("Invalid Alibaba context limit")


def install_reviewed_catalogs(
    catalogs: dict[str, dict[str, Tariff]],
    *,
    verified_at: datetime,
    expires_at: datetime,
    revision: str,
    effective_from: dict[str, dict[str, datetime]] | None = None,
) -> None:
    """Atomically replace reviewed regional prices from the trusted feed.

    The feed consumer distributes this same snapshot to every process. Native
    downloads are a fallback after the reviewed feed expires, within their TTL.
    """
    validate_reviewed_catalogs(
        catalogs,
        verified_at=verified_at,
        expires_at=expires_at,
        revision=revision,
        effective_from=effective_from,
    )
    global _reviewed_verified_at, _reviewed_expires_at, _reviewed_revision
    with _lock:
        _reviewed.clear()
        _reviewed.update(
            {region: dict(entries) for region, entries in catalogs.items()}
        )
        _reviewed_verified_at = verified_at
        _reviewed_expires_at = expires_at
        _reviewed_revision = revision
        _reviewed_effective.clear()
        _reviewed_effective.update(
            {
                region: {
                    ident: (effective_from or {})
                    .get(region, {})
                    .get(ident, verified_at)
                    for ident in entries
                }
                for region, entries in catalogs.items()
            }
        )


@dataclass(frozen=True)
class PreparedCatalogRefresh:
    """Credential snapshot prepared before releasing a database session."""

    url: str
    service_site: str
    api_key: str = field(repr=False)


class CatalogRefreshStatus(str, Enum):
    """Outcome of a native-catalog overlay refresh."""

    ingested = "ingested"
    no_target = "no_target"
    no_credentials = "no_credentials"
    host_mismatch = "host_mismatch"
    unreachable = "unreachable"
    empty = "empty"


def reset_live_state_for_tests() -> None:
    """Drop the in-process overlay (test isolation only)."""
    with _lock:
        _live.clear()
        _live_dates.clear()
        _reviewed.clear()
        _reviewed_effective.clear()


def native_catalog_target(ai_model: models.AIModel) -> tuple[str, str] | None:
    """Return the documented native catalog URL and service_site, or none.

    Singapore classic keys use the documented classic Singapore native host.
    Singapore workspace hosts still name that documented catalog URL so
    Fetch price can explain a host mismatch; refresh will not send a
    workspace key there. US workspace native is the workspace host itself.
    Classic ``dashscope-us`` has no documented native catalog URL and is not
    guessed.
    """
    if not is_alibaba(ai_model):
        return None
    host = _host(ai_model)
    if host == "dashscope-intl.aliyuncs.com" or host.endswith(
        ".ap-southeast-1.maas.aliyuncs.com"
    ):
        return SINGAPORE_NATIVE_URL, "international"
    if host.endswith(".us-east-1.maas.aliyuncs.com"):
        return f"https://{host}/api/v1/models", "united-states"
    return None


def region_key(service_site: str) -> str:
    """Stable overlay key for a USD service site."""
    if service_site == "united-states":
        return "united-states"
    return "singapore-international"


def live_tariff(
    ai_model: models.AIModel, *, observed_at: datetime | None = None
) -> Tariff | None:
    """Return a live overlay tariff for this exact model id, if present."""
    target = native_catalog_target(ai_model)
    if target is None:
        return None
    ident = (ai_model.model_identifier or "").strip()
    if not ident:
        return None
    key = region_key(target[1])
    now = _utcnow()
    when = observed_at or now
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    with _lock:
        effective = _reviewed_effective.get(key, {}).get(ident)
        if effective is not None and when < effective:
            return None
        native = _live.get(key, {}).get(ident)
        native_date = _live_dates.get((key, ident))
        if native_date is None or now - native_date >= timedelta(
            seconds=NATIVE_TTL_SECONDS
        ):
            native = None
        reviewed = _reviewed.get(key, {}).get(ident)
        # Explicitly reviewed prices remain authoritative until expiry. A
        # native fetch cannot supersede newer evidence in a mixed-date feed.
        if reviewed is not None and (
            _reviewed_expires_at is not None and now < _reviewed_expires_at
        ):
            return reviewed
        # Evidence expiry is not evidence of a price change. Prefer a fresh
        # native tariff, otherwise retain reviewed rates with stale metadata.
        return native if native is not None else reviewed


def ingest_native_models(
    entries: Iterable[Any],
    *,
    region: str = "singapore-international",
    replace: bool = False,
) -> int:
    """Merge or replace native catalog rows in the in-process overlay.

    Partial discovery ingest (Fetch Models) merges so a later page can
    add SKUs without dropping earlier ones. A complete native download
    passes ``replace=True`` to wholesale-replace the region bucket so SKUs
    that left the catalog drop.

    Args:
        entries: ``output.models`` objects from one or more pages.
        region: Overlay key, normally ``singapore-international``.
        replace: When True, the region bucket becomes exactly these tariffs.

    Returns:
        Count of models with a usable USD token tariff.
    """
    incoming: dict[str, Tariff] = {}
    for entry in entries:
        tariff = parse_native_model(entry)
        if tariff is None:
            continue
        ident = str(entry.get("model") or "").strip()
        if not ident:
            continue
        incoming[ident] = tariff
    accepted = len(incoming)
    with _lock:
        if replace:
            _live[region] = incoming
        else:
            _live.setdefault(region, {}).update(incoming)
        now = _utcnow()
        for ident in incoming:
            _live_dates[(region, ident)] = now
    return accepted


def tariff_source(ai_model: models.AIModel) -> str | None:
    """Identify the overlay that supplies the currently selected tariff."""
    tariff = live_tariff(ai_model)
    target = native_catalog_target(ai_model)
    if tariff is None or target is None:
        return None
    ident = (ai_model.model_identifier or "").strip()
    with _lock:
        if _reviewed.get(region_key(target[1]), {}).get(ident) is tariff:
            return "reviewed-catalog"
    return "native-catalog"


def native_tariff(ai_model: models.AIModel) -> Tariff | None:
    """Read a fresh native quote without overriding the active reviewed tariff."""
    target = native_catalog_target(ai_model)
    if target is None:
        return None
    region = region_key(target[1])
    ident = (ai_model.model_identifier or "").strip()
    with _lock:
        fetched = _live_dates.get((region, ident))
        if fetched is None or _utcnow() - fetched >= timedelta(
            seconds=NATIVE_TTL_SECONDS
        ):
            return None
        return _live.get(region, {}).get(ident)


def parse_native_model(entry: Any) -> Tariff | None:
    """Parse one native catalog model into a USD token tariff.

    Image, audio, and other non-token units are ignored. A time_band on an
    input or output row is kept only when both idle and busy token rates are
    present; a single band is not enough to estimate.
    """
    if not isinstance(entry, dict):
        return None
    groups = entry.get("prices")
    if not isinstance(groups, list) or not groups:
        return None
    unbanded: list[Tariff] = []
    banded: list[Tariff] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        tariff = _tariff_from_price_group(group)
        if tariff is not None:
            unbanded.append(tariff)
            continue
        both = _banded_tariff_from_price_group(group)
        if both is not None:
            banded.append(both)
    if unbanded:
        return _combine_tariff_tiers(unbanded)
    if len(banded) == 1:
        return banded[0]
    return None


def _combine_tariff_tiers(tiers: list[Tariff]) -> Tariff | None:
    if not tiers:
        return None
    if len(tiers) == 1:
        return tiers[0]
    first = tiers[0]
    return Tariff(
        input=first.input,
        output=first.output,
        implicit_read=first.implicit_read,
        explicit_read=first.explicit_read,
        creation=first.creation,
        max_input=first.max_input,
        tiers=tuple(tiers),
    )


def _normalize_time_band(value: Any) -> str | None:
    raw = str(value or "").strip().lower().replace("_", "-").replace(" ", "-")
    if raw in {"", "default", "none"}:
        return "default"
    if raw in {"busy", "peak", "daytime", "day"}:
        return "busy"
    if raw in {"idle", "off-peak", "offpeak", "night"}:
        return "idle"
    return None


def _tariff_from_price_group(
    group: dict[str, Any], *, band: str | None = None
) -> Tariff | None:
    items = group.get("prices")
    if not isinstance(items, list):
        return None
    parsed: dict[str, float] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        item_band = _normalize_time_band(item.get("time_band"))
        if band is None:
            if item_band not in (None, "default"):
                continue
        elif item_band != band:
            continue
        if not _is_token_unit(str(item.get("price_unit") or "")):
            continue
        kind = _price_type(str(item.get("type") or ""))
        if kind is None:
            continue
        try:
            amount = float(item.get("price"))
        except (TypeError, ValueError):
            continue
        if amount < 0 or not math.isfinite(amount):
            continue
        parsed[kind] = amount
    if "input" not in parsed or "output" not in parsed:
        return None
    return Tariff(
        input=parsed["input"],
        output=parsed["output"],
        implicit_read=parsed.get("implicit_read"),
        explicit_read=parsed.get("explicit_read"),
        creation=parsed.get("creation"),
        max_input=_parse_range_upper(str(group.get("range_name") or "")),
    )


def _banded_tariff_from_price_group(group: dict[str, Any]) -> Tariff | None:
    busy = _tariff_from_price_group(group, band="busy")
    idle = _tariff_from_price_group(group, band="idle")
    if busy is None or idle is None:
        return None
    return Tariff(
        input=busy.input,
        output=busy.output,
        implicit_read=busy.implicit_read,
        explicit_read=busy.explicit_read,
        creation=busy.creation,
        max_input=busy.max_input,
        time_bands=TimeBands(idle=idle, busy=busy),
    )


def _is_token_unit(unit: str) -> bool:
    compact = " ".join(unit.strip().lower().replace("-", " ").split())
    return compact in _TOKEN_UNITS


def _price_type(raw: str) -> str | None:
    kind = raw.strip().lower()
    if kind == "input_token":
        return "input"
    if kind == "output_token":
        return "output"
    if kind in {"input_token_cache", "input_token_cache_implicit"}:
        return "implicit_read"
    if kind in {"input_token_cache_read", "input_token_cache_explicit"}:
        return "explicit_read"
    if kind in {
        "input_token_cache_creation_5m",
        "input_token_cache_creation",
    }:
        return "creation"
    return None


def _parse_range_upper(range_name: str) -> int | None:
    label = range_name.strip()
    if not label or label.lower() == "default":
        return None
    parts = re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*([KMkm]?)", label.replace(",", ""))
    if not parts:
        return None
    number, unit = parts[-1]
    value = float(number)
    suffix = unit.upper()
    if suffix == "K":
        value *= 1_000
    elif suffix == "M":
        value *= 1_000_000
    return int(value)


def _credential_host_matches_catalog(
    ai_model: models.AIModel, catalog_url: str
) -> bool:
    """True when this model's configured host is the catalog we would call."""
    catalog_host = (urlparse(catalog_url).hostname or "").lower()
    return bool(catalog_host) and _host(ai_model) == catalog_host


def prepare_refresh(
    ai_model: models.AIModel,
) -> PreparedCatalogRefresh | CatalogRefreshStatus:
    """Resolve scoped credentials without performing network I/O."""
    target = native_catalog_target(ai_model)
    if target is None:
        return CatalogRefreshStatus.no_target
    url, service_site = target
    if not _credential_host_matches_catalog(ai_model, url):
        return CatalogRefreshStatus.host_mismatch
    api_key = _api_key(ai_model)
    if not api_key:
        return CatalogRefreshStatus.no_credentials
    return PreparedCatalogRefresh(url, service_site, api_key)


def refresh_prepared(
    prepared: PreparedCatalogRefresh, *, max_duration_seconds: float | None = None
) -> CatalogRefreshStatus:
    """Download an already scoped snapshot without accessing model credentials."""
    try:
        kwargs = (
            {"max_duration_seconds": max_duration_seconds}
            if max_duration_seconds is not None
            else {}
        )
        entries, complete = _download_catalog(
            prepared.url, prepared.api_key, prepared.service_site, **kwargs
        )
    except Exception:  # noqa: BLE001 - overlay refresh is best-effort
        logger.debug("Alibaba native catalog refresh failed", exc_info=True)
        return CatalogRefreshStatus.unreachable
    accepted = ingest_native_models(
        entries,
        region=region_key(prepared.service_site),
        replace=complete,
    )
    if accepted > 0:
        return CatalogRefreshStatus.ingested
    return CatalogRefreshStatus.empty if complete else CatalogRefreshStatus.unreachable


def refresh_from_model(ai_model: models.AIModel) -> CatalogRefreshStatus:
    """Prepare then refresh. Session-owning callers should use the split API."""
    prepared = prepare_refresh(ai_model)
    if isinstance(prepared, CatalogRefreshStatus):
        return prepared
    return refresh_prepared(prepared)


def install_live_tariff(region: str, model_id: str, tariff: Tariff) -> None:
    """Install one overlay tariff (tests and Fetch Models)."""
    with _lock:
        _live.setdefault(region, {})[model_id] = tariff
        _live_dates[(region, model_id)] = _utcnow()


def _download_catalog(
    url: str,
    api_key: str,
    service_site: str,
    *,
    max_duration_seconds: float = 30.0,
) -> tuple[list[dict[str, Any]], bool]:
    """Download native catalog pages.

    Returns:
        ``(entries, complete)``. ``complete`` is True only when pagination
        reached a documented end. Auth, ``success: false``, and safety
        stops yield no entries and ``complete=False`` so the overlay is
        not wiped.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return [], False
    deadline = time.monotonic() + max(0.0, max_duration_seconds)
    models_out: list[dict[str, Any]] = []
    seen_pages: set[tuple[str, ...]] = set()
    complete = False
    with httpx.Client(timeout=NATIVE_TIMEOUT_SECONDS, follow_redirects=False) as client:
        for page_no in range(1, _MAX_PAGES + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            response = client.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=min(NATIVE_TIMEOUT_SECONDS, remaining),
                params={
                    "capabilities": "TG",
                    "service_site": service_site,
                    "language": "en-US",
                    "page_no": page_no,
                    "page_size": _PAGE_SIZE,
                },
            )
            if response.status_code in {401, 403}:
                return [], False
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict) or body.get("success") is not True:
                return [], False
            output = body.get("output")
            if not isinstance(output, dict) or not isinstance(
                output.get("models"), list
            ):
                return [], False
            entries = [row for row in output["models"] if isinstance(row, dict)]
            page_ids = tuple(str(row.get("model") or "") for row in entries)
            if page_ids in seen_pages:
                break
            seen_pages.add(page_ids)
            models_out.extend(entries)
            total = output.get("total")
            if not entries or (type(total) is int and page_no * _PAGE_SIZE >= total):
                complete = True
                break
            if len(entries) < _PAGE_SIZE and type(total) is not int:
                complete = True
                break
    return models_out, complete


def _legacy_api_key(ai_model: models.AIModel) -> str | None:
    """Return a non-empty legacy ``ai_model.api_key``, if present."""
    legacy = getattr(ai_model, "api_key", None)
    if isinstance(legacy, str) and legacy.strip():
        return legacy.strip()
    return None


def _api_key(ai_model: models.AIModel) -> str | None:
    try:
        from preloop.services.secret_service import get_secret_service

        resolved = get_secret_service().resolve_ai_model_credentials(
            ai_model, allow_refresh=False
        )
        if (
            resolved is not None
            and resolved.credential_type == "api_key"
            and isinstance(resolved.value, str)
            and resolved.value.strip()
        ):
            return resolved.value.strip()
    except Exception:  # noqa: BLE001 - optional secret backends
        logger.debug("Alibaba catalog could not resolve credentials", exc_info=True)
        return _legacy_api_key(ai_model)
    return _legacy_api_key(ai_model)


def reviewed_before_effective(
    ai_model: models.AIModel, *, observed_at: datetime | None = None
) -> bool:
    """Fail closed before the only available reviewed tariff revision."""
    target = native_catalog_target(ai_model)
    if target is None:
        return False
    ident = (ai_model.model_identifier or "").strip()
    when = observed_at or _utcnow()
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    with _lock:
        effective = _reviewed_effective.get(region_key(target[1]), {}).get(ident)
        return effective is not None and when < effective


def pricing_snapshot(
    ai_model: models.AIModel,
    *,
    observed_at: datetime | None = None,
    prompt_tokens: int = 0,
    usage_details: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Expose reviewed provenance for an applicable tariff's usage record."""
    from preloop.services.alibaba_pricing import (
        _SEED,
        _SEED_CACHE_DATES,
        tariff_for_usage,
    )

    ident = (ai_model.model_identifier or "").strip()
    selected = tariff_for_usage(
        ai_model,
        prompt_tokens=prompt_tokens,
        usage_details=usage_details,
        observed_at=observed_at,
    )
    if selected is not None and selected is _SEED.get(ident):
        stamp = _SEED_CACHE_DATES.get(ident)
        return {
            "provider": "alibaba",
            "region": "singapore-international",
            "source": "seed",
            "cache_effective_from": stamp.isoformat() if stamp else None,
        }
    tariff = live_tariff(ai_model, observed_at=observed_at)
    target = native_catalog_target(ai_model)
    if tariff is None or target is None:
        return None
    region = region_key(target[1])
    ident = (ai_model.model_identifier or "").strip()
    with _lock:
        if _reviewed.get(region, {}).get(ident) is not tariff:
            return {"provider": "alibaba", "region": region, "source": "native-catalog"}
        return {
            "provider": "alibaba",
            "region": region,
            "source": "reviewed-catalog",
            "revision": _reviewed_revision,
            "stale": _reviewed_expires_at is not None
            and _utcnow() >= _reviewed_expires_at,
            "verified_at": _reviewed_verified_at.isoformat()
            if _reviewed_verified_at
            else None,
            "effective_from": _reviewed_effective[region][ident].isoformat(),
            "expires_at": _reviewed_expires_at.isoformat()
            if _reviewed_expires_at
            else None,
        }

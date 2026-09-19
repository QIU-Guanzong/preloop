"""Region-scoped Alibaba Model Studio list-price estimates, never invoice cost.

Singapore International USD seed: ``data/alibaba_international_prices.json``,
from the native ``GET /api/v1/models`` catalog. The live overlay
(see ``alibaba_price_catalog``) keeps that map current, including cache rows
from the same USD site response. Chat completions still report tokens only;
image, TTS, and other unit rates use matching usage fields.

Beijing and other CNY sites stay unpriced in USD accounting. Time-banded
Singapore International SKUs use Model Studio night hours (22:00-08:00
UTC+8, idle) versus daytime (busy). Do not substitute a native
DeepSeek/Z.ai/Moonshot price for an Alibaba-hosted model.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from preloop.models import models
from preloop.services.litellm_routing import is_openrouter_model

SEED_PATH = (
    Path(__file__).resolve().parent / "data" / "alibaba_international_prices.json"
)


UTC8 = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class Tariff:
    """USD list prices for one serving tariff, tier, or non-token unit."""

    input: float | None = None
    output: float | None = None
    implicit_read: float | None = None
    explicit_read: float | None = None
    creation: float | None = None
    max_input: int | None = None
    tiers: tuple["Tariff", ...] = ()
    time_bands: TimeBands | None = None
    per_image: float | None = None
    per_image_input: float | None = None
    per_image_output: float | None = None
    per_second: float | None = None
    per_10k_characters: float | None = None
    per_voice: float | None = None
    extra_rates: tuple[tuple[str, str, float], ...] = ()

    def has_token_rates(self) -> bool:
        """True when this tariff can estimate prompt and completion tokens."""
        return self.input is not None and self.output is not None


@dataclass(frozen=True)
class TimeBands:
    """Model Studio night/daytime token tariffs for one SKU."""

    idle: Tariff
    busy: Tariff


def is_alibaba_idle_hour(when: datetime) -> bool:
    """True during Model Studio night hours, 22:00-08:00 UTC+8."""
    aware = when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)
    hour = aware.astimezone(UTC8).hour
    return hour >= 22 or hour < 8


def select_time_band(tariff: Tariff, observed_at: datetime | None) -> Tariff:
    """Pick the idle or busy tariff for the request time, or the flat tariff."""
    bands = tariff.time_bands
    if bands is None:
        return tariff
    when = observed_at or datetime.now(timezone.utc)
    chosen = bands.idle if is_alibaba_idle_hour(when) else bands.busy
    return chosen


def applied_time_band(tariff: Tariff, observed_at: datetime | None) -> str | None:
    """Return ``idle`` or ``busy`` when this tariff is time-banded."""
    if tariff.time_bands is None:
        return None
    when = observed_at or datetime.now(timezone.utc)
    return "idle" if is_alibaba_idle_hour(when) else "busy"


def _host(ai_model: models.AIModel) -> str:
    endpoint = getattr(ai_model, "api_endpoint", None)
    if not isinstance(endpoint, str):
        return ""
    try:
        return (urlparse(endpoint.strip()).hostname or "").lower()
    except ValueError:
        return ""


def is_alibaba(ai_model: models.AIModel) -> bool:
    """Recognize provider identity or an actual Alibaba serving hostname."""
    if is_openrouter_model(ai_model):
        return False
    host = _host(ai_model)
    return (ai_model.provider_name or "").strip().lower() in {"qwen", "dashscope"} or (
        host
        in {
            "dashscope.aliyuncs.com",
            "dashscope-intl.aliyuncs.com",
            "dashscope-us.aliyuncs.com",
            "cn-hongkong.dashscope.aliyuncs.com",
        }
        or host.endswith(".maas.aliyuncs.com")
    )


def usd_region(ai_model: models.AIModel) -> str | None:
    """Return the USD overlay/seed region, or none when currency is unverified."""
    host = _host(ai_model)
    if host == "dashscope-intl.aliyuncs.com" or host.endswith(
        ".ap-southeast-1.maas.aliyuncs.com"
    ):
        return "singapore-international"
    if host.endswith(".us-east-1.maas.aliyuncs.com"):
        return "united-states"
    return None


def _unit_fields_from_seed(entry: dict[str, Any]) -> dict[str, Any]:
    """Copy non-token list prices from a seed object."""
    payload: dict[str, Any] = {}
    for key in (
        "per_image",
        "per_image_input",
        "per_image_output",
        "per_second",
        "per_10k_characters",
        "per_voice",
    ):
        rate = _optional_rate(entry.get(key))
        if rate is not None:
            payload[key] = rate
    extra = entry.get("extra_rates")
    if isinstance(extra, list):
        parsed: list[tuple[str, str, float]] = []
        for row in extra:
            if not isinstance(row, dict):
                continue
            kind = str(row.get("type") or "").strip()
            unit = str(row.get("unit") or "").strip()
            amount = _optional_rate(row.get("amount"))
            if kind and unit and amount is not None:
                parsed.append((kind, unit, amount))
        if parsed:
            payload["extra_rates"] = tuple(parsed)
    return payload


def _tariff_from_seed_entry(entry: dict[str, Any]) -> Tariff | None:
    units = _unit_fields_from_seed(entry)
    bands = entry.get("time_bands")
    if isinstance(bands, dict):
        idle = _tariff_from_seed_tiers(bands.get("idle"))
        busy = _tariff_from_seed_tiers(bands.get("busy"))
        if idle is None or busy is None:
            return None
        return Tariff(
            input=busy.input,
            output=busy.output,
            implicit_read=busy.implicit_read,
            explicit_read=busy.explicit_read,
            creation=busy.creation,
            max_input=busy.max_input,
            tiers=busy.tiers,
            time_bands=TimeBands(idle=idle, busy=busy),
            **units,
        )
    token = _tariff_from_seed_tiers(entry)
    if token is not None:
        if not units:
            return token
        return Tariff(
            input=token.input,
            output=token.output,
            implicit_read=token.implicit_read,
            explicit_read=token.explicit_read,
            creation=token.creation,
            max_input=token.max_input,
            tiers=token.tiers,
            **units,
        )
    if units:
        return Tariff(**units)
    return None


def _tariff_from_seed_tiers(entry: Any) -> Tariff | None:
    if not isinstance(entry, dict):
        return None
    raw_tiers = entry.get("tiers")
    if not isinstance(raw_tiers, list) or not raw_tiers:
        return None
    implicit = _optional_rate(entry.get("implicit_read"))
    explicit = _optional_rate(entry.get("explicit_read"))
    creation = _optional_rate(entry.get("creation"))
    parsed: list[Tariff] = []
    for row in raw_tiers:
        if not isinstance(row, dict):
            continue
        try:
            inp = float(row["input"])
            out = float(row["output"])
        except (KeyError, TypeError, ValueError):
            continue
        if inp < 0 or out < 0 or not math.isfinite(inp) or not math.isfinite(out):
            continue
        max_input = row.get("max_input")
        parsed.append(
            Tariff(
                input=inp,
                output=out,
                implicit_read=_optional_rate(row.get("implicit_read")) or implicit,
                explicit_read=_optional_rate(row.get("explicit_read")) or explicit,
                creation=_optional_rate(row.get("creation")) or creation,
                max_input=int(max_input) if isinstance(max_input, int) else None,
            )
        )
    if not parsed:
        return None
    first = parsed[0]
    return Tariff(
        input=first.input,
        output=first.output,
        implicit_read=first.implicit_read,
        explicit_read=first.explicit_read,
        creation=first.creation,
        max_input=first.max_input,
        tiers=tuple(parsed) if len(parsed) > 1 else (),
    )


def _optional_rate(value: Any) -> float | None:
    if value is None:
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if rate < 0 or not math.isfinite(rate):
        return None
    return rate


def _load_seed() -> dict[str, Tariff]:
    try:
        payload = json.loads(SEED_PATH.read_text())
    except (OSError, ValueError):
        return {}
    models_raw = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models_raw, dict):
        return {}
    loaded: dict[str, Tariff] = {}
    for ident, entry in models_raw.items():
        if not isinstance(ident, str) or not isinstance(entry, dict):
            continue
        tariff = _tariff_from_seed_entry(entry)
        if tariff is not None:
            loaded[ident.strip()] = tariff
    return loaded


_SEED = _load_seed()


def _load_seed_cache_dates() -> dict[str, datetime]:
    """Read independently dated console cache evidence without dating base rates."""
    try:
        entries = json.loads(SEED_PATH.read_text()).get("models", {})
    except (OSError, ValueError, AttributeError):
        return {}
    if not isinstance(entries, dict):
        return {}
    dates = {}
    for ident, entry in entries.items():
        if not isinstance(entry, dict) or "cache_effective_from" not in entry:
            continue
        try:
            stamp = datetime.fromisoformat(
                entry["cache_effective_from"].replace("Z", "+00:00")
            )
        except (TypeError, ValueError, AttributeError):
            continue
        if stamp.tzinfo is not None:
            dates[ident] = stamp
    return dates


_SEED_CACHE_DATES = _load_seed_cache_dates()


def _seed_cache_predates_evidence(
    ai_model: models.AIModel,
    tariff: Tariff,
    observed_at: datetime | None,
) -> bool:
    """Do not apply a current console cache quote to an unverified older period."""
    ident = (ai_model.model_identifier or "").strip()
    effective = _SEED_CACHE_DATES.get(ident)
    if effective is None or tariff is not _SEED.get(ident) or observed_at is None:
        return False
    when = (
        observed_at
        if observed_at.tzinfo is not None
        else observed_at.replace(tzinfo=timezone.utc)
    )
    return when < effective


def tariff_for(
    ai_model: models.AIModel, *, observed_at: datetime | None = None
) -> Tariff | None:
    """Resolve an exact SKU on a USD Alibaba host.

    Live native-catalog overlay wins. The Singapore International seed covers
    models before the first refresh. Other USD regions require a live overlay.
    """
    region = usd_region(ai_model)
    if region is None:
        return None
    ident = (ai_model.model_identifier or "").strip()
    if not ident:
        return None
    from preloop.services.alibaba_price_catalog import (
        live_tariff,
        reviewed_before_effective,
    )

    if reviewed_before_effective(ai_model, observed_at=observed_at):
        return None
    live = live_tariff(ai_model, observed_at=observed_at)
    if live is not None:
        return live
    # Concurrent install_reviewed_catalogs() must not fall through to seed.
    if reviewed_before_effective(ai_model, observed_at=observed_at):
        return None
    if region == "singapore-international":
        return _SEED.get(ident)
    return None


def catalog_entry(ai_model: models.AIModel) -> tuple[str, dict[str, Any]] | None:
    """Expose the same scoped list tariff in the model pricing view."""
    parent = tariff_for(ai_model)
    if parent is None:
        return None
    tariff = select_time_band(parent, None)
    region = usd_region(ai_model) or "singapore-international"
    entry: dict[str, Any] = {}
    if tariff.input is not None:
        entry["input_cost_per_token"] = tariff.input / 1_000_000
    if tariff.output is not None:
        entry["output_cost_per_token"] = tariff.output / 1_000_000
    # The UI has one cached-input column. Do not collapse explicit and implicit
    # prices into one misleading number when they differ.
    if tariff.implicit_read is not None and tariff.explicit_read in (
        None,
        tariff.implicit_read,
    ):
        entry["cache_read_input_token_cost"] = tariff.implicit_read / 1_000_000
    if tariff.per_image is not None:
        entry["input_cost_per_image"] = tariff.per_image
    if tariff.per_image_input is not None:
        entry["input_cost_per_image"] = tariff.per_image_input
    if tariff.per_image_output is not None:
        entry["output_cost_per_image"] = tariff.per_image_output
    if tariff.per_second is not None:
        entry["output_cost_per_second"] = tariff.per_second
    if tariff.per_10k_characters is not None:
        entry["output_cost_per_10k_characters"] = tariff.per_10k_characters
    if tariff.per_voice is not None:
        entry["output_cost_per_voice"] = tariff.per_voice
    if tariff.extra_rates:
        entry["alibaba_extra_rates"] = [
            {"type": kind, "unit": unit, "amount": amount}
            for kind, unit, amount in tariff.extra_rates
        ]
    band = applied_time_band(parent, None)
    if band is not None:
        entry["time_band"] = band
    if not entry:
        return None
    from preloop.services.alibaba_price_catalog import tariff_source

    prefix = f"alibaba/{tariff_source(ai_model) or region}"
    return f"{prefix}/{ai_model.model_identifier}", entry


def select_tier(tariff: Tariff, prompt_tokens: int) -> Tariff | None:
    """Pick the whole-request input-length tier, or none if out of range."""
    if tariff.tiers:
        matching = [
            tier
            for tier in tariff.tiers
            if tier.max_input is None or prompt_tokens <= tier.max_input
        ]
        if not matching:
            return None
        matching.sort(key=lambda tier: tier.max_input or 10**18)
        return matching[0]
    if tariff.max_input is not None and prompt_tokens > tariff.max_input:
        return None
    return tariff


def tariff_for_usage(
    ai_model: models.AIModel,
    *,
    prompt_tokens: int,
    usage_details: dict[str, Any] | None,
    observed_at: datetime | None = None,
) -> Tariff | None:
    """Keep a partial native listing from hiding an identical verified seed policy."""
    from preloop.services.alibaba_price_catalog import tariff_source

    resolved = tariff_for(ai_model, observed_at=observed_at)
    if resolved is None or tariff_source(ai_model) != "native-catalog":
        return resolved
    seed = _SEED.get((ai_model.model_identifier or "").strip())
    if (
        seed is None
        or usd_region(ai_model) != "singapore-international"
        or _seed_cache_predates_evidence(ai_model, seed, observed_at)
    ):
        return resolved

    # Select the entire seed only when every base tier/bound agrees. Never
    # splice cached rates from a different native price or context policy.
    def signature(tariff: Tariff) -> tuple[tuple[float, float, int | None], ...]:
        return tuple(
            (t.input, t.output, t.max_input) for t in (tariff.tiers or (tariff,))
        )

    native_rows = resolved.tiers or (resolved,)
    seed_rows = seed.tiers or (seed,)
    flat_unspecified_bound = (
        len(native_rows) == len(seed_rows) == 1
        and resolved.max_input is None
        and resolved.input == seed.input
        and resolved.output == seed.output
        and select_tier(seed, prompt_tokens) is not None
    )
    if signature(seed) != signature(resolved) and not flat_unspecified_bound:
        return resolved
    # Missing context metadata on a single flat native row may use the seed
    # only within its known bound. Known native cache rates must all agree.
    for native_row, seed_row in zip(native_rows, seed_rows, strict=True):
        for field in ("implicit_read", "explicit_read", "creation"):
            known_rate = getattr(native_row, field)
            if known_rate is not None and known_rate != getattr(seed_row, field):
                return resolved
    native_tier = select_tier(resolved, prompt_tokens)
    seed_tier = select_tier(seed, prompt_tokens)
    if native_tier is None or seed_tier is None:
        return resolved
    usage = usage_details or {}
    details = usage.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        return resolved
    try:
        cached = int(details.get("cached_tokens") or 0)
        nested = details.get("cache_creation") or {}
        if not isinstance(nested, dict):
            return resolved
        created = int(
            details.get("cache_creation_input_tokens")
            or details.get("cache_creation_tokens")
            or nested.get("ephemeral_5m_input_tokens")
            or usage.get("cache_creation_input_tokens")
            or 0
        )
    except (ValueError, TypeError, OverflowError):
        return resolved
    mode = usage.get("_preloop_cache_mode")
    read_field = "explicit_read" if mode == "explicit" else "implicit_read"
    needs_read = cached > 0 and mode in {"implicit", "explicit"}
    seed_covers = (not needs_read or getattr(seed_tier, read_field) is not None) and (
        not created or seed_tier.creation is not None
    )
    native_missing = (needs_read and getattr(native_tier, read_field) is None) or (
        created > 0 and native_tier.creation is None
    )
    return seed if seed_covers and native_missing else resolved


def estimate(
    ai_model: models.AIModel,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    usage_details: dict[str, Any] | None,
    observed_at: datetime | None = None,
) -> float | None:
    """Estimate known token classes; unknown tariff/mode yields no estimate.

    Prompt totals already include cached and creation tokens. Completion totals
    already include reasoning tokens, which must never be added a second time.
    The internal cache-mode tag comes from the forwarded request, not the model.
    """
    parent = tariff_for_usage(
        ai_model,
        prompt_tokens=prompt_tokens,
        usage_details=usage_details,
        observed_at=observed_at,
    )
    if parent is None:
        return None
    resolved = select_time_band(parent, observed_at)
    tariff = select_tier(resolved, prompt_tokens)
    if tariff is None:
        return None
    if not tariff.has_token_rates():
        return _estimate_non_token(tariff, usage_details)
    if _mixed_modality_usage(tariff, usage_details):
        return None
    usage = usage_details or {}
    details = usage.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        return None
    creation_details = details.get("cache_creation") or {}
    if not isinstance(creation_details, dict):
        return None
    try:
        cached = int(details.get("cached_tokens") or 0)
        created = int(
            details.get("cache_creation_input_tokens")
            or details.get("cache_creation_tokens")
            or creation_details.get("ephemeral_5m_input_tokens")
            or usage.get("cache_creation_input_tokens")
            or 0
        )
    except (TypeError, ValueError, OverflowError):
        return None
    if min(prompt_tokens, completion_tokens, cached, created) < 0:
        return None
    if cached + created > prompt_tokens:
        return None
    if (cached or created) and _seed_cache_predates_evidence(
        ai_model, parent, observed_at
    ):
        return None
    mode = usage.get("_preloop_cache_mode")
    read_rate = tariff.explicit_read if mode == "explicit" else tariff.implicit_read
    if cached and (mode not in {"implicit", "explicit"} or read_rate is None):
        return None
    if created and tariff.creation is None:
        return None
    return round(
        (
            (prompt_tokens - cached - created) * tariff.input
            + cached * (read_rate or 0)
            + created * (tariff.creation or 0)
            + completion_tokens * tariff.output
        )
        / 1_000_000,
        6,
    )


def _mixed_modality_usage(tariff: Tariff, usage_details: dict[str, Any] | None) -> bool:
    """True when leftover audio/vision rates cannot be applied to known tokens."""
    extra = {kind.lower() for kind, _, _ in tariff.extra_rates}
    if not extra:
        return False
    details = (usage_details or {}).get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        return False

    def _positive(*keys: str) -> bool:
        for key in keys:
            raw = details.get(key)
            if raw in (None, 0):
                continue
            try:
                return int(raw) > 0
            except (TypeError, ValueError, OverflowError):
                return True
        return False

    if any("audio" in kind for kind in extra) and _positive("audio_tokens"):
        return True
    if any(
        token in kind for kind in extra for token in ("image", "vision")
    ) and _positive("image_tokens", "vision_tokens"):
        return True
    return False


def _usage_int(usage: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key not in usage:
            continue
        try:
            value = int(usage[key])
        except (TypeError, ValueError, OverflowError):
            return None
        if value < 0:
            return None
        return value
    return None


def _estimate_non_token(
    tariff: Tariff, usage_details: dict[str, Any] | None
) -> float | None:
    """Estimate image, audio duration, TTS, or voice units from usage details."""
    usage = usage_details or {}
    if tariff.per_image is not None:
        count = _usage_int(
            usage, "image_count", "n_images", "output_images", "num_images"
        )
        if count is None:
            return None
        return round(count * tariff.per_image, 6)
    if tariff.per_image_input is not None or tariff.per_image_output is not None:
        inputs = _usage_int(usage, "input_images", "image_count")
        outputs = _usage_int(usage, "output_images", "n_images")
        if inputs is None and outputs is None:
            return None
        return round(
            (inputs or 0) * (tariff.per_image_input or 0)
            + (outputs or 0) * (tariff.per_image_output or 0),
            6,
        )
    if tariff.per_second is not None:
        seconds = _usage_int(usage, "duration_seconds", "audio_seconds", "seconds")
        if seconds is None:
            return None
        return round(seconds * tariff.per_second, 6)
    if tariff.per_10k_characters is not None:
        chars = _usage_int(usage, "character_count", "characters", "tts_characters")
        if chars is None:
            return None
        return round(chars * tariff.per_10k_characters / 10_000, 6)
    if tariff.per_voice is not None:
        voices = _usage_int(usage, "voice_count", "voices")
        if voices is None:
            return None
        return round(voices * tariff.per_voice, 6)
    return None


def pricing_failure_reason(
    ai_model: models.AIModel,
    *,
    prompt_tokens: int = 0,
    usage_details: dict[str, Any] | None = None,
    observed_at: datetime | None = None,
) -> str | None:
    """Explain an unsupported pricing dimension without guessing a tariff."""
    from preloop.services.alibaba_price_catalog import reviewed_before_effective

    if usd_region(ai_model) is None:
        return "unsupported_region"
    if reviewed_before_effective(ai_model, observed_at=observed_at):
        return "tariff_not_effective"
    parent = tariff_for_usage(
        ai_model,
        prompt_tokens=prompt_tokens,
        usage_details=usage_details,
        observed_at=observed_at,
    )
    if parent is None:
        return "missing_model_tariff"
    resolved = select_time_band(parent, observed_at)
    tariff = select_tier(resolved, prompt_tokens)
    if tariff is None:
        return "context_out_of_range"
    if not tariff.has_token_rates():
        if _estimate_non_token(tariff, usage_details) is None:
            return "non_token_usage_required"
        return None
    if _mixed_modality_usage(tariff, usage_details):
        return "mixed_modality_usage"
    usage = usage_details or {}
    details = usage.get("prompt_tokens_details") or {}
    if not isinstance(details, dict):
        return "invalid_usage"
    creation = details.get("cache_creation") or {}
    if not isinstance(creation, dict):
        return "invalid_usage"
    try:
        cached = int(details.get("cached_tokens") or 0)
        created = int(
            details.get("cache_creation_input_tokens")
            or details.get("cache_creation_tokens")
            or creation.get("ephemeral_5m_input_tokens")
            or usage.get("cache_creation_input_tokens")
            or 0
        )
    except (TypeError, ValueError, OverflowError):
        return "invalid_usage"
    if min(prompt_tokens, cached, created) < 0 or cached + created > prompt_tokens:
        return "invalid_usage"
    if (cached or created) and _seed_cache_predates_evidence(
        ai_model, parent, observed_at
    ):
        return "cache_tariff_not_effective"
    mode = usage.get("_preloop_cache_mode")
    if cached:
        if mode not in {"implicit", "explicit"}:
            return "unknown_cache_mode"
        rate = tariff.explicit_read if mode == "explicit" else tariff.implicit_read
        if rate is None:
            return f"missing_{mode}_cache_tariff"
    if created and tariff.creation is None:
        return "missing_cache_creation_tariff"
    return None

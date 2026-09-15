#!/usr/bin/env python3
"""Measure what gateway usage indexing costs per concurrent large response.

Issue #670: gateway replicas were OOMKilled while three agents were calling
``/openai/v1/responses`` at once, and the candidates for the growth had to be
attributed rather than guessed. This script measures the response-path work
in isolation, so the numbers in ``docs/operations/gateway-memory.md`` can be
reproduced without a cluster.

Two modes are measured for the same payloads:

``current``
    What the gateway does now: build one bounded document and queue it, so
    the payloads are free as soon as the document exists.

``legacy``
    The pre-fix path, kept here verbatim enough to be a fair comparison: a
    sanitized deep copy of both payloads, then whitespace-normalizing every
    value in full before truncating it.

Optionally (``--with-seal-pass``, needs ``DATABASE_URL``) the audit chain
seal pass runs in the same process while indexing runs, which is the
"background pass in-process" half of the comparison.

Examples
--------
  PRELOOP_DISABLE_TELEMETRY=true python3 scripts/measure_gateway_index_memory.py
  PRELOOP_DISABLE_TELEMETRY=true python3 scripts/measure_gateway_index_memory.py \\
      --concurrency 3 --payload-kb 1024 --json /tmp/gateway-memory.json
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import sys
import threading
import tracemalloc
from datetime import timedelta
from typing import Any, Optional

os.environ.setdefault("PRELOOP_DISABLE_TELEMETRY", "true")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

from preloop.models.models.api_usage import ApiUsage  # noqa: E402
from preloop.services.gateway_usage_search import (  # noqa: E402
    GatewayUsageSearchService,
)

MIB = 1024 * 1024


def _rss_bytes() -> int:
    """Maximum resident set size of this process, in bytes."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes, macOS bytes.
    return peak if sys.platform == "darwin" else peak * 1024


def _usage_row() -> ApiUsage:
    return ApiUsage(
        id="00000000-0000-0000-0000-000000000670",
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=1.0,
        provider_name="openai",
        model_alias="openai/gpt-5",
        meta_data={"endpoint_kind": "responses", "requested_model": "openai/gpt-5"},
    )


def _payload_pair(payload_chars: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """One realistic large interaction: a long prompt and a long answer."""
    filler = "token " * (payload_chars // 6)
    request_payload = {
        "model": "openai/gpt-5",
        "instructions": "summarize the attached context",
        "input": filler,
        "api_key": "sk-placeholder",
    }
    response_payload = {
        "id": "resp_placeholder",
        "output_text": filler,
        "output": [{"type": "message", "content": filler}],
        "usage": {"input_tokens": 1000, "output_tokens": 1000},
    }
    return request_payload, response_payload


def _legacy_sanitize(value: Any) -> Any:
    """The deep copy the response path used to make before flattening."""
    if value is None:
        return None
    if isinstance(value, dict):
        if value.get("redacted") is True:
            return None
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if GatewayUsageSearchService._is_secret_key(key):
                sanitized[key] = GatewayUsageSearchService.REDACTED_VALUE
                continue
            sanitized_item = _legacy_sanitize(item)
            if sanitized_item is not None:
                sanitized[key] = sanitized_item
        return sanitized or None
    if isinstance(value, list):
        items = [item for item in (_legacy_sanitize(i) for i in value) if item]
        return items or None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) > GatewayUsageSearchService.MAX_VALUE_CHARS:
        return text[: GatewayUsageSearchService.MAX_VALUE_CHARS] + "... [truncated]"
    return text


def _index_current(usage: ApiUsage, request: dict, response: dict) -> int:
    document = GatewayUsageSearchService().build_index_document(
        usage=usage, request_payload=request, response_payload=response
    )
    return 0 if document is None else len(document.searchable_text)


def _index_legacy(usage: ApiUsage, request: dict, response: dict) -> int:
    text = GatewayUsageSearchService().build_searchable_text(
        usage=usage,
        request_payload=_legacy_sanitize(request),
        response_payload=_legacy_sanitize(response),
    )
    return len(text)


def _run_mode(
    mode: str,
    *,
    concurrency: int,
    payload_chars: int,
    seal_pass: bool,
    seal_lag_seconds: Optional[int] = None,
) -> dict[str, Any]:
    """Run ``concurrency`` indexing passes at once and report the peak."""
    index = _index_current if mode == "current" else _index_legacy
    payloads = [_payload_pair(payload_chars) for _ in range(concurrency)]
    usage = _usage_row()
    document_chars: list[int] = []
    lock = threading.Lock()
    ready = threading.Barrier(concurrency)
    seal_summary: Optional[dict[str, Any]] = None

    def _worker(slot: int) -> None:
        request_payload, response_payload = payloads[slot]
        ready.wait(timeout=30)
        size = index(usage, request_payload, response_payload)
        with lock:
            document_chars.append(size)

    rss_before = _rss_bytes()
    tracemalloc.start()
    try:
        seal_thread = None
        if seal_pass:
            seal_result: dict[str, Any] = {}

            def _seal() -> None:
                from preloop.models.db.session import get_db_session
                from preloop.services.audit_chain import run_seal_pass

                lag = (
                    None
                    if seal_lag_seconds is None
                    else timedelta(seconds=seal_lag_seconds)
                )
                db = next(get_db_session())
                try:
                    seal_result.update(
                        run_seal_pass(db, ignore_enabled=True, lag=lag).as_dict()
                    )
                finally:
                    db.close()

            seal_thread = threading.Thread(target=_seal, name="seal-pass")
            seal_thread.start()

        threads = [
            threading.Thread(target=_worker, args=(slot,))
            for slot in range(concurrency)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=300)
        if seal_thread is not None:
            seal_thread.join(timeout=300)
            seal_summary = seal_result
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    return {
        "mode": mode,
        "concurrency": concurrency,
        "payload_kib": round(payload_chars / 1024, 1),
        "peak_traced_mib": round(peak / MIB, 2),
        "peak_traced_mib_per_response": round(peak / MIB / concurrency, 2),
        "rss_growth_mib": round((_rss_bytes() - rss_before) / MIB, 2),
        "document_chars": document_chars,
        "seal_pass": seal_summary,
    }


def seed_audit_history(*, accounts: int, rows_per_account: int) -> None:
    """Create throwaway accounts with unsealed audit rows.

    Only for a disposable database: a seal pass over an empty database
    measures nothing, and the incident happened with a couple of hundred
    accounts in the table.
    """
    from preloop.models.crud import crud_account, crud_audit_log
    from preloop.models.db.session import get_db_session

    db = next(get_db_session())
    try:
        for index in range(accounts):
            account = crud_account.create(
                db,
                obj_in={
                    "organization_name": f"Measurement Account {index}",
                    "is_active": True,
                },
            )
            for row in range(rows_per_account):
                crud_audit_log.log_action(
                    db,
                    account_id=str(account.id),
                    action="measurement_event",
                    resource_type="measurement",
                    resource_id=str(row),
                    status="success",
                    details={"row": row},
                    commit=False,
                )
            db.commit()
        print(f"seeded {accounts} accounts x {rows_per_account} audit rows")
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument(
        "--payload-kb",
        type=int,
        default=1024,
        help="Size of the prompt and of the answer, each, in KiB",
    )
    parser.add_argument(
        "--with-seal-pass",
        action="store_true",
        help="Run the audit chain seal pass in-process (needs DATABASE_URL)",
    )
    parser.add_argument(
        "--seed-accounts",
        type=int,
        default=0,
        help="Synthetic accounts to create first, so the seal pass has work",
    )
    parser.add_argument(
        "--seed-audit-rows",
        type=int,
        default=200,
        help="Unsealed audit rows per seeded account",
    )
    parser.add_argument(
        "--seal-lag-seconds",
        type=int,
        default=None,
        help="Override how old a row must be before the seal pass chains it",
    )
    parser.add_argument("--json", dest="json_path", default=None)
    args = parser.parse_args()

    if args.with_seal_pass and not os.environ.get("DATABASE_URL"):
        parser.error("--with-seal-pass needs DATABASE_URL")
    if args.seed_accounts:
        if not os.environ.get("DATABASE_URL"):
            parser.error("--seed-accounts needs DATABASE_URL")
        seed_audit_history(
            accounts=args.seed_accounts, rows_per_account=args.seed_audit_rows
        )

    payload_chars = args.payload_kb * 1024
    results = [
        _run_mode(
            mode,
            concurrency=args.concurrency,
            payload_chars=payload_chars,
            seal_pass=False,
        )
        for mode in ("legacy", "current")
    ]
    if args.with_seal_pass:
        results.append(
            _run_mode(
                "current",
                concurrency=args.concurrency,
                payload_chars=payload_chars,
                seal_pass=True,
                seal_lag_seconds=args.seal_lag_seconds,
            )
        )

    header = (
        f"{'mode':>8}  {'seal':>5}  {'peak MiB':>9}  {'per resp':>9}  {'RSS MiB':>8}"
    )
    print(header)
    print("-" * len(header))
    for result in results:
        print(
            f"{result['mode']:>8}  "
            f"{('yes' if result['seal_pass'] is not None else 'no'):>5}  "
            f"{result['peak_traced_mib']:>9}  "
            f"{result['peak_traced_mib_per_response']:>9}  "
            f"{result['rss_growth_mib']:>8}"
        )
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            json.dump(results, handle, indent=2)
        print(f"wrote {args.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

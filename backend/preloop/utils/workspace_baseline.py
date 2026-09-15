"""Deliver a previous run's stored result into ``/workspace`` as a baseline.

The review preset family (architecture conformance, repo code health,
standards walk) computes drift against a *baseline*: a prior run's
``result.json``. Until now the only way to deliver one was the
``workspace_files`` seed, which is capped at 96 KiB base64 per file (one
``execve`` string) and is a human dropping a file into a payload. Neither
fits a scheduled re-run that should diff against its own last run with
nobody in the loop.

This module carries the other half of that link: the trigger payload names
a previous execution by id
(:data:`PREVIOUS_RESULT_EXECUTION_ID_KEY`, optionally the sentinel
:data:`PREVIOUS_RUN_SENTINEL`), the orchestrator resolves it *inside the
same account* (see ``preloop.services.workspace_baseline``), and the
resolved result is written into the workspace at
:data:`BASELINE_WORKSPACE_PATH` before the agent starts.

Transport
---------

Same shape as the workspace seed: content travels in the container
environment, never in the launch command, because the command is a single
``execve`` string capped at ``MAX_ARG_STRLEN`` (128 KiB) and shared with
the rendered prompt. Unlike a seed, a baseline may exceed what one
environment variable can hold, so it is split into
:data:`BASELINE_CHUNK_ENCODED_BYTES` base64 chunks, one variable each, and
reassembled in order inside the container. Splitting base64 at arbitrary
offsets and concatenating in order reproduces the exact encoded text, so
no chunk needs to decode on its own.

Caps
----

:data:`MAX_BASELINE_RESULT_BYTES` (256 KiB of serialized JSON) bounds what
is delivered. The review presets keep ``result.json`` under 200 KB, so the
cap sits above the contract it serves, and above it the run degrades
instead of truncating: truncated JSON is not a baseline, it is a parse
error inside the agent.

Degrading
---------

Anything that stops a baseline from being delivered (an id that does not
resolve in this account, an execution with no stored result, a result over
the cap) writes :data:`BASELINE_MISMATCH_PATH` instead: a small JSON marker
the preset reads to set ``baseline_mismatch`` and keep ``drift`` null. The
run still starts and still succeeds. An id belonging to another account is
treated exactly like one that does not exist, down to the reason string, so
the marker cannot be used to probe for foreign execution ids.

Precedence
----------

An explicitly delivered baseline file wins. If the payload names
``previous_result_path``/``previous_result_url``, or seeds a file at
:data:`BASELINE_WORKSPACE_PATH` through ``workspace_files``, the execution
id is ignored and nothing is written here: the caller who attached a file
said what they wanted, and two baselines in one workspace is not a run
anyone asked for.
"""

from __future__ import annotations

import base64
import json
import shlex
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, model_validator

from preloop.utils.workspace_seed import (
    MAX_SINGLE_SEED_ENCODED_BYTES,
    WORKSPACE_FILES_KEY,
    WORKSPACE_ROOT,
    workspace_containment_shell_body,
)

# Key on the trigger payload naming the execution whose stored result is the
# baseline for this run. Accepts an execution id or PREVIOUS_RUN_SENTINEL.
PREVIOUS_RESULT_EXECUTION_ID_KEY = "previous_result_execution_id"

# Sentinel value for "this flow's own most recent run that reported a
# result". A scheduled subscription cannot know an id in advance, and a
# fixed id would pin every future run to one frozen baseline.
PREVIOUS_RUN_SENTINEL = "last"

# Payload keys that deliver a baseline explicitly; either one wins over the
# execution id (see module docstring).
PREVIOUS_RESULT_PATH_KEY = "previous_result_path"
PREVIOUS_RESULT_URL_KEY = "previous_result_url"

# Where the baseline lands, relative to /workspace. This is the path the
# review presets already document for a seeded baseline.
BASELINE_WORKSPACE_PATH = "previous/result.json"

# Where the mismatch marker lands when no baseline could be delivered.
BASELINE_MISMATCH_PATH = "previous/baseline-mismatch.json"

# Serialized-JSON bytes allowed for a delivered baseline.
MAX_BASELINE_RESULT_BYTES = 256 * 1024  # 256 KiB

# Environment variables carrying the base64 chunks of the baseline.
BASELINE_ENV_PREFIX = "PRELOOP_WORKSPACE_BASELINE_"

# Base64 bytes per chunk: one environment variable per chunk, under the
# same MAX_ARG_STRLEN budget a single workspace seed gets.
BASELINE_CHUNK_ENCODED_BYTES = MAX_SINGLE_SEED_ENCODED_BYTES

# Mismatch reasons recorded on the marker and on the execution context.
# "unavailable" covers unknown, foreign and malformed ids alike: the run
# must not be able to tell them apart.
MISMATCH_UNAVAILABLE = "previous_result_unavailable"
MISMATCH_NO_RESULT = "previous_result_missing"
MISMATCH_TOO_LARGE = "previous_result_too_large"


class BaselineDelivery(BaseModel):
    """What the runner will write into the workspace for this run.

    Either a baseline (``content_base64`` set, ``mismatch_reason`` None) or
    a mismatch marker (the reverse). Never both, never neither.
    """

    path: str = BASELINE_WORKSPACE_PATH
    marker_path: str = BASELINE_MISMATCH_PATH
    content_base64: str = ""
    mismatch_reason: Optional[str] = None
    # Id of the execution the baseline came from, for the run's audit trail.
    # Never set for a mismatch: a resolved id is the only one worth naming.
    source_execution_id: Optional[str] = None

    @model_validator(mode="after")
    def _exactly_one_of_content_or_mismatch(self) -> "BaselineDelivery":
        """Refuse the empty-shell case: never both, never neither."""
        has_content = bool(self.content_base64)
        has_reason = bool(self.mismatch_reason)
        if has_content == has_reason:
            raise ValueError(
                "BaselineDelivery requires exactly one of content_base64 "
                "or mismatch_reason; never both, never neither"
            )
        return self

    @property
    def delivered(self) -> bool:
        """True when an actual baseline (not a marker) will be written."""
        return not self.mismatch_reason


def baseline_delivery(
    content: bytes, *, source_execution_id: Optional[str] = None
) -> BaselineDelivery:
    """Delivery carrying ``content`` (serialized JSON) as the baseline."""
    return BaselineDelivery(
        content_base64=base64.b64encode(content).decode("ascii"),
        source_execution_id=source_execution_id,
    )


def baseline_mismatch(reason: str) -> BaselineDelivery:
    """Delivery carrying a mismatch marker instead of a baseline."""
    return BaselineDelivery(mismatch_reason=reason)


def serialize_baseline(result: Any) -> bytes:
    """Serialize a stored execution result to the bytes written on disk.

    ``FlowExecution.result`` is JSONB, so it round-trips through
    ``json.dumps``; ``default=str`` keeps an exotic value (a datetime a
    caller stored) from failing a run that is only meant to degrade.
    """
    return json.dumps(result, default=str).encode("utf-8")


def _payload_and_top_level(
    trigger_event_data: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """The two mappings a trigger key may live on, payload first.

    Same lookup order ``workspace_seed_payload`` uses for
    ``workspace_files``: inside ``payload`` first, then beside it. Callers
    that put the key in both get the inner one; the two places are one
    request, not two runs, so there is nothing to reconcile.
    """
    if not isinstance(trigger_event_data, dict):
        return []
    mappings: List[Dict[str, Any]] = []
    payload = trigger_event_data.get("payload")
    if isinstance(payload, dict):
        mappings.append(payload)
    mappings.append(trigger_event_data)
    return mappings


def previous_result_execution_id(
    trigger_event_data: Optional[Dict[str, Any]],
) -> Optional[str]:
    """The requested previous-result execution id, if the payload names one.

    Returns the raw string as given (including the ``last`` sentinel);
    validation belongs to the resolver, which degrades rather than raising.
    """
    for mapping in _payload_and_top_level(trigger_event_data):
        value = mapping.get(PREVIOUS_RESULT_EXECUTION_ID_KEY)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None:
            # A non-string (a dict, a list, a number) is not an id. Treat it
            # as "requested but unusable" so the run gets a marker rather
            # than silently no baseline at all.
            return ""
    return None


def explicit_baseline_declared(
    trigger_event_data: Optional[Dict[str, Any]],
    seed_paths: Optional[List[str]] = None,
) -> bool:
    """Whether the caller delivered a baseline file explicitly.

    True when the payload names ``previous_result_path`` or
    ``previous_result_url``, or when ``workspace_files`` seeds a file at
    the baseline path. Either way the execution id stands down.
    """
    for path in seed_paths or []:
        if path == BASELINE_WORKSPACE_PATH:
            return True
    for mapping in _payload_and_top_level(trigger_event_data):
        for key in (PREVIOUS_RESULT_PATH_KEY, PREVIOUS_RESULT_URL_KEY):
            value = mapping.get(key)
            if isinstance(value, str) and value.strip():
                return True
        # A seed declared beside the payload is still a seed.
        declared = mapping.get(WORKSPACE_FILES_KEY)
        if isinstance(declared, list):
            for entry in declared:
                if (
                    isinstance(entry, dict)
                    and str(entry.get("path", "")).strip() == BASELINE_WORKSPACE_PATH
                ):
                    return True
    return False


def baseline_chunk_env_var(index: int) -> str:
    """Name of the environment variable carrying baseline chunk ``index``."""
    return f"{BASELINE_ENV_PREFIX}{index}"


def baseline_chunks(content_base64: str) -> List[str]:
    """Split base64 text into per-variable chunks, in order."""
    size = BASELINE_CHUNK_ENCODED_BYTES
    return [
        content_base64[offset : offset + size]
        for offset in range(0, len(content_base64), size)
    ]


def baseline_env(delivery: Optional[BaselineDelivery]) -> Dict[str, str]:
    """Environment carrying the baseline, one variable per base64 chunk.

    Empty for a mismatch marker: the marker is small, fixed text that the
    shell block writes literally, with no content to keep out of the
    launch command.
    """
    if delivery is None or not delivery.delivered:
        return {}
    return {
        baseline_chunk_env_var(index): chunk
        for index, chunk in enumerate(baseline_chunks(delivery.content_base64))
    }


def _containment_helper() -> str:
    """POSIX helper that validates one target path under ``$w``.

    Same guard as the workspace seed prelude, from the shared helper in
    ``workspace_seed``: resolve the deepest existing ancestor physically
    and require it to stay inside the workspace root, then refuse to
    write through a symlink at the target itself. A cloned repository
    may contain ``previous -> /etc``, and lexical validation cannot see
    that.
    """
    return (
        "__pl_baseline_dir() { " + workspace_containment_shell_body("baseline") + "; }"
    )


def build_workspace_baseline_shell(
    delivery: Optional[BaselineDelivery], workspace_root: str = WORKSPACE_ROOT
) -> str:
    """Build the init-command block that writes the baseline or its marker.

    Runs after git clone (whose pre-clone backup would sweep earlier writes
    away) and before the workspace seeds, so an explicitly seeded file at
    the same path still wins even if precedence were ever resolved twice.

    Baseline bytes are *not* in this block: each chunk is read from the
    environment variable :func:`baseline_chunk_env_var` names, reassembled
    into one base64 file under ``/tmp`` and decoded onto the target. A
    chunk that arrives empty aborts rather than writing a half baseline,
    because half a baseline reads as a resolved finding for everything it
    is missing.
    """
    if delivery is None:
        return ""

    resolve_root = (
        f"w0={shlex.quote(workspace_root)}; "
        'w="$(cd -P "$w0" 2>/dev/null && pwd || printf \'%s\' "$w0")"'
    )

    if not delivery.delivered:
        marker = json.dumps(
            {"baseline_mismatch": True, "reason": delivery.mismatch_reason}
        )
        body = (
            f"__pl_baseline_dir {shlex.quote(delivery.marker_path)}; "
            f"printf '%s' {shlex.quote(marker)} > \"$t\"; "
            f'rm -f "$w"/{shlex.quote(delivery.path)}'
        )
        return f"( set -e; {resolve_root}; {_containment_helper()}; {body} )"

    chunks = baseline_chunks(delivery.content_base64)
    appends = "; ".join(
        (
            f'[ -n "${baseline_chunk_env_var(index)}" ] || {{ '
            f'echo "baseline: chunk {index} not delivered" >&2; exit 1; }}; '
            f'printf \'%s\' "${baseline_chunk_env_var(index)}" >> "$b"'
        )
        for index in range(len(chunks))
    )
    body = (
        f"__pl_baseline_dir {shlex.quote(delivery.path)}; "
        'b="$(mktemp)"; '
        f"{appends}; "
        'base64 -d < "$b" > "$t"; rm -f "$b"; '
        f'rm -f "$w"/{shlex.quote(delivery.marker_path)}'
    )
    return f"( set -e; {resolve_root}; {_containment_helper()}; {body} )"

"""Object shapes for flow to flow delegation.

One flow asking another to run produces three records: the request the
parent sends, the task record the child execution is represented by, and
the artifact the child's result becomes. This module freezes those shapes
so every child of #621 implements against one vocabulary.

The shapes follow the A2A Protocol Specification v1.0.1 (protocol version
1.0), JSON serialised the way that specification requires (ProtoJSON, per
its ADR-001): camelCase member names, ``SCREAMING_SNAKE_CASE`` enum
values, and union members discriminated by the member name rather than by
a ``kind`` field. They are shaped for the JSON-RPC binding, the one
Preloop would expose first if it ever served A2A.

This is internal delegation in A2A shapes, not A2A support. There is no
JSON-RPC endpoint, no Agent Card, no external counterparty and no
streaming event here, and this module adds no behaviour: it loads JSON,
maps a status and validates a dictionary. See
``docs/guide/flow-delegation-shapes.md``.
"""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, Final, Mapping, Tuple

#: Directory holding the frozen JSON schemas.
SCHEMA_DIR: Final[Path] = Path(__file__).resolve().parent / "schemas"

#: Exact specification release these shapes were written against.
A2A_SPECIFICATION_VERSION: Final[str] = "1.0.1"

#: Protocol version as A2A itself names it: ``Major.Minor`` of the above.
A2A_PROTOCOL_VERSION: Final[str] = "1.0"

#: The binding the shapes are written for, spelled as an Agent Card would
#: spell it. Nothing is served over it today.
A2A_PROTOCOL_BINDING: Final[str] = "JSONRPC"

#: Synthetic status for a delegation Preloop refused. It is never stored on
#: a ``flow_execution`` row: a refused call creates no child execution.
REFUSED_STATUS: Final[str] = "REFUSED"

#: Preloop execution status to A2A task state. Every status the executor
#: writes has a row; an unmapped status raises rather than defaulting, so a
#: new status cannot silently arrive as "working".
STATUS_TO_TASK_STATE: Final[Mapping[str, str]] = MappingProxyType(
    {
        # Accepted, nothing running yet: no container, no model turn.
        "PENDING": "TASK_STATE_SUBMITTED",
        "INITIALIZING": "TASK_STATE_SUBMITTED",
        "STARTING": "TASK_STATE_SUBMITTED",
        # The agent is working, including a parked run being restarted.
        "RUNNING": "TASK_STATE_WORKING",
        "RESUMING": "TASK_STATE_WORKING",
        # Alive, holding no runtime, waiting on a human answer. Interrupted
        # in A2A terms, not terminal: the child can still finish.
        "WAITING_FOR_HUMAN": "TASK_STATE_INPUT_REQUIRED",
        # Terminal, the work was done.
        "SUCCEEDED": "TASK_STATE_COMPLETED",
        # Terminal, the work was attempted and ended badly. A timeout and an
        # infrastructure abort are failures: nobody asked for them.
        "FAILED": "TASK_STATE_FAILED",
        "TIMEOUT": "TASK_STATE_FAILED",
        "TIMED_OUT": "TASK_STATE_FAILED",
        "ABORTED": "TASK_STATE_FAILED",
        # Terminal, someone asked for the stop. Both spellings appear in the
        # executor and both mean the same thing.
        "STOPPED": "TASK_STATE_CANCELED",
        "CANCELLED": "TASK_STATE_CANCELED",
        "CANCELED": "TASK_STATE_CANCELED",
        # Not a failure: policy declined to start the child at all, so no
        # tokens were spent and there is nothing to retry blindly.
        REFUSED_STATUS: "TASK_STATE_REJECTED",
    }
)

#: Why a delegation was refused. Frozen so a parent can branch on the
#: reason instead of matching on prose.
REFUSAL_REASONS: Final[Tuple[str, ...]] = (
    "flow_not_found",
    "flow_not_callable",
    "tool_not_allowed",
    "firewall_denied",
    "depth_exceeded",
    "cycle_detected",
    "fanout_exceeded",
    "budget_exceeded",
)

#: Every ``preloop.ai/`` metadata key these records may carry. The schemas
#: allow exactly these and the document describes exactly these; a test
#: asserts all three agree.
DELEGATION_METADATA_KEYS: Final[Tuple[str, ...]] = (
    "preloop.ai/kind",
    "preloop.ai/executionId",
    "preloop.ai/parentExecutionId",
    "preloop.ai/rootExecutionId",
    "preloop.ai/flowId",
    "preloop.ai/flowName",
    "preloop.ai/depth",
    "preloop.ai/status",
    "preloop.ai/cost",
    "preloop.ai/tokens",
    "preloop.ai/consoleUrl",
    "preloop.ai/refusalReason",
)

#: Schema file names, without the suffix.
REQUEST_SCHEMA: Final[str] = "delegation_request"
TASK_SCHEMA: Final[str] = "delegation_task"


class DelegationShapeError(ValueError):
    """A record does not match the frozen delegation shapes."""


@lru_cache(maxsize=None)
def _cached_schema(name: str) -> Dict[str, Any]:
    """Load and cache one frozen schema by name."""
    path = SCHEMA_DIR / f"{name}.schema.json"
    if not path.is_file():
        raise DelegationShapeError(f"No delegation schema named {name!r}")
    with path.open(encoding="utf-8") as handle:
        schema: Dict[str, Any] = json.load(handle)
    return schema


def load_schema(name: str) -> Dict[str, Any]:
    """Load one frozen schema by name.

    Returns a deep copy of the cached parse so a caller cannot poison
    later validation by mutating the dict.

    Args:
        name: ``delegation_request`` or ``delegation_task``.

    Returns:
        The parsed JSON schema.

    Raises:
        DelegationShapeError: If no schema of that name is shipped.
    """
    return copy.deepcopy(_cached_schema(name))


def _validate(payload: Mapping[str, Any], schema_name: str) -> None:
    """Validate one record, reporting the first fault with its path."""
    # Imported here, not at module import: jsonschema is a development
    # dependency and nothing on the request path validates today.
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(_cached_schema(schema_name))
    errors = sorted(validator.iter_errors(payload), key=lambda error: error.path)
    if not errors:
        return
    first = errors[0]
    location = "/".join(str(part) for part in first.path) or "<root>"
    raise DelegationShapeError(f"{schema_name} invalid at {location}: {first.message}")


def validate_delegation_request(payload: Mapping[str, Any]) -> None:
    """Validate the request a parent sends when it delegates.

    Args:
        payload: The A2A shaped message, already JSON compatible.

    Raises:
        DelegationShapeError: If the record does not match the schema.
    """
    _validate(payload, REQUEST_SCHEMA)


def validate_delegation_task(payload: Mapping[str, Any]) -> None:
    """Validate the task record standing for one child execution.

    Args:
        payload: The A2A shaped task, already JSON compatible.

    Raises:
        DelegationShapeError: If the record does not match the schema.
    """
    _validate(payload, TASK_SCHEMA)


def task_state_for_status(status: str) -> str:
    """Map a Preloop execution status to its A2A task state.

    Args:
        status: An execution status, or ``REFUSED`` for a call policy
            declined to start.

    Returns:
        The A2A task state name.

    Raises:
        DelegationShapeError: If the status has no row in the table. A new
            status is a decision, not a default.
    """
    try:
        return STATUS_TO_TASK_STATE[status]
    except KeyError:
        raise DelegationShapeError(
            f"No task state mapped for execution status {status!r}: add a row "
            "to STATUS_TO_TASK_STATE and to docs/guide/flow-delegation-shapes.md"
        ) from None


def is_refusal(status: str) -> bool:
    """Whether this status means the child was never started.

    A refusal is not a failure. Nothing ran, nothing was charged, and the
    parent is told which rule declined it.
    """
    return task_state_for_status(status) == "TASK_STATE_REJECTED"

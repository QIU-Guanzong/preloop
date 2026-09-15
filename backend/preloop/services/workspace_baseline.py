"""Resolve the trigger payload's previous-result execution id into a baseline.

The payload key and the transport live in
``preloop.utils.workspace_baseline``; this is the account-scoped read that
turns an id into bytes. Called once while the execution context is built,
so the decision (deliver, or mark a mismatch) is made before any agent
starts and is recorded on the context either way.

Account scoping is the CRUD layer's, not ours: the lookup passes the
flow's ``account_id``, so an execution in another account simply does not
come back, and the caller cannot tell it apart from an id that never
existed. A missing ``account_id`` is unresolvable, not unscoped: the
column is nullable, and an unfiltered get would deliver a foreign
result.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from preloop.models.crud import crud_flow_execution
from preloop.utils.workspace_baseline import (
    MAX_BASELINE_RESULT_BYTES,
    MISMATCH_NO_RESULT,
    MISMATCH_TOO_LARGE,
    MISMATCH_UNAVAILABLE,
    PREVIOUS_RUN_SENTINEL,
    BaselineDelivery,
    baseline_delivery,
    baseline_mismatch,
    explicit_baseline_declared,
    previous_result_execution_id,
    serialize_baseline,
)

logger = logging.getLogger(__name__)


def resolve_baseline_delivery(
    db: Session,
    *,
    trigger_event_data: Optional[Dict[str, Any]],
    account_id: Any,
    flow_id: Any = None,
    exclude_execution_id: Any = None,
    seed_paths: Optional[List[str]] = None,
) -> Optional[BaselineDelivery]:
    """Resolve the payload's baseline request for one execution.

    Returns ``None`` when nothing should be written: either the payload
    names no previous result, or it delivers one explicitly (a seeded file
    or a URL), which takes precedence over the id.

    Otherwise returns a delivery: the previous run's stored result when it
    resolves inside this account and fits the cap, and a mismatch marker in
    every other case. This function does not raise on a bad id; a run that
    cannot find its baseline is a run without drift, not a failed run.
    """
    requested = previous_result_execution_id(trigger_event_data)
    if requested is None:
        return None
    if explicit_baseline_declared(trigger_event_data, seed_paths):
        logger.info(
            "Ignoring previous_result_execution_id: the payload delivers a "
            "baseline file explicitly, which takes precedence"
        )
        return None
    if not requested:
        return baseline_mismatch(MISMATCH_UNAVAILABLE)

    execution = _resolve_execution(
        db,
        requested=requested,
        account_id=account_id,
        flow_id=flow_id,
        exclude_execution_id=exclude_execution_id,
    )
    if execution is None:
        # Unknown, foreign or malformed: one reason for all three, so the
        # marker never reports on another account's data.
        logger.info("Baseline execution did not resolve in this account")
        return baseline_mismatch(MISMATCH_UNAVAILABLE)
    if execution.result is None:
        logger.info("Baseline execution %s stored no result", execution.id)
        return baseline_mismatch(MISMATCH_NO_RESULT)

    content = serialize_baseline(execution.result)
    if len(content) > MAX_BASELINE_RESULT_BYTES:
        logger.info(
            "Baseline execution %s stored %d bytes, over the %d byte cap",
            execution.id,
            len(content),
            MAX_BASELINE_RESULT_BYTES,
        )
        return baseline_mismatch(MISMATCH_TOO_LARGE)

    logger.info(
        "Delivering baseline from execution %s (%d bytes)", execution.id, len(content)
    )
    return baseline_delivery(content, source_execution_id=str(execution.id))


def _resolve_execution(
    db: Session,
    *,
    requested: str,
    account_id: Any,
    flow_id: Any,
    exclude_execution_id: Any,
) -> Optional[Any]:
    """The execution the payload asked for, or None if it is not readable."""
    # A missing account is not "no filter": CRUD get / latest_with_result
    # skip the Flow join when account_id is falsy, which would read
    # across tenants. Degrade instead.
    if not account_id:
        return None
    scope = str(account_id)
    if requested == PREVIOUS_RUN_SENTINEL:
        if not flow_id:
            return None
        return crud_flow_execution.latest_with_result(
            db,
            flow_id=flow_id,
            account_id=scope,
            exclude_execution_id=exclude_execution_id,
        )
    try:
        execution_id = uuid.UUID(requested)
    except (ValueError, AttributeError, TypeError):
        return None
    return crud_flow_execution.get(db, id=execution_id, account_id=scope)

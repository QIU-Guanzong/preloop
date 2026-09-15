"""Account scoped validation for the ``Flow.callable_flows`` allowlist.

Delegation fails closed: NULL or ``[]`` on the column means a flow may call
nothing at all, and every entry has to name a flow inside the account that owns
the calling flow. This module is the write side guard, called from the flow
endpoints. Nothing enforces the allowlist at call time yet (issue #627 is the
column, the validation and the API).

Resolution of a reference, in order, always inside the calling account:

1. a flow in the account whose name matches exactly, then case insensitively;
2. a preset catalog slug, resolved to the account's flow of that preset name,
   then to the account's clone of that global preset.

A reference that resolves to nothing inside the account is rejected, which is
also what happens to a reference naming a flow in another account: from here
the two are the same failure, and the message names the entry either way.
"""

import logging
from typing import Any, List, Optional

from sqlalchemy import String, cast, func
from sqlalchemy.orm import Session

from preloop.flow_presets import PRESET_SLUGS
from preloop.models.crud import crud_flow
from preloop.models.models.flow import Flow
from preloop.models.schemas.flow import CallableFlowEntry, parse_callable_flows

logger = logging.getLogger(__name__)


class CallableFlowsError(ValueError):
    """An allowlist an operator cannot have meant. Fail closed on write."""


def _flow_by_name_case_insensitive(
    db: Session, *, name: str, account_id: Any
) -> Optional[Flow]:
    """Find an account flow whose name matches ignoring case."""
    return (
        db.query(Flow)
        .filter(
            func.lower(Flow.name) == name.lower(),
            cast(Flow.account_id, String) == str(account_id),
        )
        .order_by(Flow.created_at.asc())
        .first()
    )


def resolve_callable_flow(
    db: Session, *, reference: str, account_id: Any
) -> Optional[Flow]:
    """Resolve one allowlist reference inside ``account_id``.

    Args:
        db: Database session.
        reference: Slug or name written in the allowlist entry.
        account_id: Account that owns the calling flow.

    Returns:
        The flow the reference names, or None when the account has no such
        flow. Never returns a flow owned by another account.
    """
    if account_id is None:
        return None
    exact = crud_flow.get_by_name_and_account(db, name=reference, account_id=account_id)
    if exact is not None:
        return exact
    insensitive = _flow_by_name_case_insensitive(
        db, name=reference, account_id=account_id
    )
    if insensitive is not None:
        return insensitive

    preset_name = PRESET_SLUGS.get(reference)
    if preset_name is None:
        return None
    by_preset_name = crud_flow.get_by_name_and_account(
        db, name=preset_name, account_id=account_id
    )
    if by_preset_name is not None:
        return by_preset_name
    global_preset = crud_flow.get_global_preset_by_name(db, name=preset_name)
    if global_preset is None:
        return None
    return crud_flow.get_by_source_preset(
        db, account_id=account_id, source_preset_id=global_preset.id
    )


def validate_callable_flows(
    db: Session,
    *,
    callable_flows: Any,
    account_id: Any,
    flow_id: Any = None,
    flow_name: Optional[str] = None,
) -> List[CallableFlowEntry]:
    """Validate an allowlist about to be written to a flow row.

    Shape, duplicates and ceilings are already enforced by the schema; this is
    the half that needs the database: every entry resolves to a flow in the
    same account, and a self reference needs the explicit ``allow_self`` flag.

    Args:
        db: Database session.
        callable_flows: The incoming list (or None), as entries or dicts.
        account_id: Account that owns the calling flow.
        flow_id: Id of the calling flow, when it already exists.
        flow_name: Name the calling flow will have after the write.

    Returns:
        The validated entries; ``[]`` for both NULL and an empty list.

    Raises:
        CallableFlowsError: An entry names no flow in the account, or names the
            calling flow without ``allow_self``.
    """
    entries = parse_callable_flows(callable_flows)
    if not entries:
        return []
    if account_id is None:
        raise CallableFlowsError(
            "callable_flows needs an owning account: a global preset cannot "
            "resolve an allowlist entry"
        )

    for entry in entries:
        target = resolve_callable_flow(db, reference=entry.flow, account_id=account_id)
        names_self = (
            target is not None
            and flow_id is not None
            and (str(target.id) == str(flow_id))
        )
        if flow_name and entry.flow.casefold() == flow_name.casefold():
            names_self = True
        if names_self:
            if not entry.allow_self:
                raise CallableFlowsError(
                    f"callable_flows entry '{entry.flow}' is this flow itself; "
                    "set allow_self on the entry to allow recursion"
                )
            continue
        if target is None:
            raise CallableFlowsError(
                f"callable_flows entry '{entry.flow}' does not name a flow in "
                "this account"
            )
    return entries

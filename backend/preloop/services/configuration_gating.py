"""Edition policy for new advanced configuration; basic governance stays in core."""

from collections.abc import Callable
import json
from typing import Any
from fastapi import HTTPException
from sqlalchemy.orm import Session

_authorizer: Callable[[Session, str, str], None] | None = None


def register_configuration_authorizer(
    authorizer: Callable[[Session, str, str], None] | None,
) -> None:
    global _authorizer
    _authorizer = authorizer


def authorize_advanced_configuration(
    db: Session, account_id: str, capability: str
) -> None:
    if _authorizer is None:
        raise HTTPException(
            status_code=402, detail={"code": "upgrade_required", "feature": capability}
        )
    _authorizer(db, account_id, capability)


def configuration_capabilities(db: Session, account_id: str) -> dict[str, bool]:
    result = {"basic_budgets": True, "single_user_approvals": True}
    for name, capability in (
        ("advanced_budget_administration", "rbac"),
        ("advanced_approvals", "team_approvals"),
    ):
        try:
            authorize_advanced_configuration(db, account_id, capability)
            result[name] = True
        except HTTPException as error:
            if error.status_code != 402:
                raise
            result[name] = False
    return result


def authorize_team_approval_configuration(
    db: Session, account_id: str, config: dict[str, Any], previous: Any = None
) -> None:
    """Gate advanced changes, preserving basic one-person approvals and execution."""
    for key in (
        "approver_user_ids",
        "approver_team_ids",
        "escalation_user_ids",
        "escalation_team_ids",
        "escalation_workflow_id",
        "workflow_type",
        "workflow_config",
        "approvals_required",
    ):
        value = config.get(key)
        if (
            not value
            or key == "workflow_type"
            and value == "simple"
            or key == "approvals_required"
            and value <= 1
        ):
            continue
        if key == "approver_user_ids" and len(set(map(str, value))) <= 1:
            continue
        before = getattr(previous, key, None) if previous is not None else None
        if key.endswith("_ids"):
            value = sorted(map(str, value))
            before = sorted(map(str, before or []))
        if json.dumps(value, sort_keys=True, default=str) == json.dumps(
            before, sort_keys=True, default=str
        ):
            continue
        authorize_advanced_configuration(db, account_id, "team_approvals")
        return


def authorize_budget_configuration(
    db: Session, account_id: str, config: dict[str, Any], previous: Any = None
) -> None:
    """Basic limits are core; organizational scopes and notification routing are paid."""
    subject = config.get("subject_type", getattr(previous, "subject_type", None))
    advanced = subject in {"user", "team"} or any(
        config.get(key, getattr(previous, key, False))
        for key in ("notify_on_soft", "notify_on_hard")
    )
    if advanced:
        authorize_advanced_configuration(db, account_id, "rbac")

"""Optional account-capacity policy called inside the resource transaction."""

from collections.abc import Callable
from typing import Literal
from sqlalchemy.orm import Session

Resource = Literal["agents", "users"]
_authorizer: Callable[[Session, str, Resource], None] | None = None


def register_capacity_authorizer(
    authorizer: Callable[[Session, str, Resource], None] | None,
) -> None:
    """Install the edition policy; core remains unlimited without a policy."""
    global _authorizer
    _authorizer = authorizer


def authorize_capacity(db: Session, account_id: str, resource: Resource) -> None:
    """Reserve by holding the account lock through the caller's transaction."""
    if _authorizer is not None:
        _authorizer(db, account_id, resource)


_change_observer: Callable[[Session, str], None] | None = None


def register_capacity_change_observer(
    observer: Callable[[Session, str], None] | None,
) -> None:
    """Register transactional billing-outbox work for a seat mutation."""
    global _change_observer
    _change_observer = observer


def record_capacity_change(db: Session, account_id: str) -> None:
    """Mark pending billing work in the same transaction as a user change."""
    if _change_observer is not None:
        _change_observer(db, account_id)

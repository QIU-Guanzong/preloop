"""Explicit transaction boundaries for an HTTP-owned model gateway session."""

from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.orm import Session


def release_gateway_session(db: Session) -> None:
    """Commit preparation and close the worker's short database unit.

    Only an HTTP request that owns its entire session may use this boundary.
    Internal gateways can share a caller transaction and must not call it.
    Credential refresh must finish its serialized rotation before this boundary.

    Values crossing this boundary must already be immutable scalar snapshots.
    Never materialize or retain credential-bearing ORM graphs for stream pulls.
    """
    try:
        # Runtime-session and OAuth sibling preparation writes must persist.
        # This unit is owned by one worker, never an internal caller transaction.
        if db.in_transaction():
            db.commit()
    finally:
        db.close()


def has_runtime_session_summary_columns(db: Session) -> bool:
    """Inspect schema using the existing checkout, never a second pool slot."""
    columns = {
        column["name"]
        for column in inspect(db.connection()).get_columns("runtime_session")
    }
    return {"summary", "summary_updated_at"}.issubset(columns)

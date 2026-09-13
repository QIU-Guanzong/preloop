"""Protect account policy and billing intent from whole-metadata replacement.

Revision ID: 20260912_history_floor
Revises: 20260912_billing_ops

Stop old purge workers before enabling new plans. Old binaries only read the
compatibility metadata and must not run after authoritative promises diverge.
"""

from datetime import UTC, datetime
from typing import Any

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260912_history_floor"
down_revision = "20260912_billing_ops"
branch_labels = None
depends_on = None


def _backfill_values(metadata: Any) -> dict[str, Any]:
    """Tolerate malformed legacy metadata without inventing a billing intent."""
    metadata = metadata if isinstance(metadata, dict) else {}
    history = metadata.get("subscription_history_retention_days")
    if type(history) is not int or not (history == -1 or 0 < history <= 2147483647):
        history = None
    generation = metadata.get("billing_seat_sync_generation")
    if not isinstance(generation, str) or not 0 < len(generation) <= 36:
        generation = None
    attempted = metadata.get("billing_seat_sync_attempted_at")
    try:
        attempted = (
            datetime.fromisoformat(attempted) if isinstance(attempted, str) else None
        )
        if attempted is not None and attempted.tzinfo is None:
            attempted = attempted.replace(tzinfo=UTC)
    except ValueError:
        attempted = None
    pending = metadata.get("billing_pending_change")
    return {
        "subscription_history_retention_days": history,
        "billing_seat_sync_pending": metadata.get("billing_seat_sync_pending") is True,
        "billing_seat_sync_generation": generation,
        "billing_seat_sync_attempted_at": attempted,
        "billing_pending_change": pending if isinstance(pending, dict) else None,
    }


def upgrade() -> None:
    # Avoid blocking account traffic behind a busy long-running transaction.
    op.execute("SET LOCAL lock_timeout = '5s'")
    columns = [
        sa.Column("subscription_history_retention_days", sa.Integer(), nullable=True),
        sa.Column(
            "billing_seat_sync_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("billing_seat_sync_generation", sa.String(36), nullable=True),
        sa.Column(
            "billing_seat_sync_attempted_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("billing_pending_change", postgresql.JSONB(), nullable=True),
    ]
    for column in columns:
        op.add_column("account", column)
    account = sa.table(
        "account",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("meta_data", sa.JSON()),
        *[sa.column(column.name, column.type) for column in columns],
    )
    connection = op.get_bind()
    # Batches bound memory only; DDL locks remain until transaction commit.
    # Only legacy policy holders need an UPDATE. Ordinary accounts retain the
    # column defaults without extra WAL/row writes.
    after = None
    while True:
        query = (
            sa.select(account.c.id, account.c.meta_data)
            .where(
                sa.cast(account.c.meta_data, postgresql.JSONB()).has_any(
                    postgresql.array([column.name for column in columns])
                )
            )
            .order_by(account.c.id)
            .limit(500)
        )
        if after is not None:
            query = query.where(account.c.id > after)
        rows = connection.execute(query).all()
        if not rows:
            break
        for account_id, metadata in rows:
            connection.execute(
                account.update()
                .where(account.c.id == account_id)
                .values(**_backfill_values(metadata))
            )
        after = rows[-1][0]


def downgrade() -> None:
    # Forward-only operational rollout: do not downgrade after new policy writes.
    for name in (
        "billing_pending_change",
        "billing_seat_sync_attempted_at",
        "billing_seat_sync_generation",
        "billing_seat_sync_pending",
        "subscription_history_retention_days",
    ):
        op.drop_column("account", name)

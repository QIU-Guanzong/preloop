"""Add durable hosted balances without guessing historical credit.

Revision ID: 20260912_hosted_spend
Revises: 20260912_history_floor
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260912_hosted_spend"
down_revision = "20260912_history_floor"
branch_labels = None
depends_on = None


def _common():
    return [
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "hosted_spend_account",
        *_common(),
        sa.Column("lifetime_spent", sa.Numeric(20, 10), nullable=True),
        sa.Column("lifetime_reserved", sa.Numeric(20, 10), nullable=False),
        sa.Column("coverage_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("baseline_evidence", sa.String(500), nullable=True),
        sa.UniqueConstraint("account_id"),
    )
    op.create_table(
        "hosted_spend_month",
        *_common(),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("spent", sa.Numeric(20, 10), nullable=False),
        sa.Column("reserved", sa.Numeric(20, 10), nullable=False),
        sa.UniqueConstraint("account_id", "period", name="uq_hosted_spend_month"),
    )
    op.create_table(
        "hosted_spend_reservation",
        *_common(),
        sa.Column("operation_key", sa.String(128), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("reserved", sa.Numeric(20, 10), nullable=False),
        sa.Column("actual", sa.Numeric(20, 10), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.UniqueConstraint(
            "account_id", "operation_key", name="uq_hosted_spend_operation"
        ),
    )
    op.create_index(
        "ix_hosted_spend_reservation_account_id",
        "hosted_spend_reservation",
        ["account_id"],
    )
    for table in (
        "hosted_spend_account",
        "hosted_spend_month",
        "hosted_spend_reservation",
    ):
        op.create_index(f"ix_{table}_id", table, ["id"])


def downgrade() -> None:
    for table in (
        "hosted_spend_reservation",
        "hosted_spend_month",
        "hosted_spend_account",
    ):
        op.drop_table(table)

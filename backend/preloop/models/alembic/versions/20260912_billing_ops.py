"""Add durable billing operation state and subscription snapshots.

Revision ID: 20260912_billing_ops
Revises: 20260910_operator_notes
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260912_billing_ops"
down_revision = "20260910_operator_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscription",
        sa.Column(
            "billing_state", postgresql.JSONB(), nullable=False, server_default="{}"
        ),
    )
    op.create_table(
        "billing_operation",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("account.id"),
            nullable=False,
        ),
        sa.Column("operation_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", "operation_key", name="uq_billing_operation_key"
        ),
    )
    op.create_index(
        "ix_billing_operation_account_id", "billing_operation", ["account_id"]
    )


def downgrade() -> None:
    op.drop_table("billing_operation")
    op.drop_column("subscription", "billing_state")

"""Execution lineage columns on flow_execution (parent, root, depth).

Additive and backfill-free. The three columns land with no writer: an
execution started from a trigger is its own root, so a row that predates
lineage reads back as no parent, no root and depth 0, which is exactly what
the default records for new rows.

Backfill rule for existing rows: ``delegation_depth`` 0, both id columns NULL.
Nothing derived them historically: a retry, a continuation and a park resume
all get their own row, and these columns are the first place a chain is
recorded, so inventing parents for old rows would be guessing.

Revision ID: 20260915_flow_execution_lineage
Revises: 20260914_pricing_merge
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260915_flow_execution_lineage"
down_revision: Union[str, None] = "20260914_pricing_merge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the lineage columns, their indexes and the self-referencing FK."""
    op.add_column(
        "flow_execution",
        sa.Column(
            "parent_execution_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment=(
                "The execution that started this one, when another execution "
                "did; NULL for a trigger-started run"
            ),
        ),
    )
    op.add_column(
        "flow_execution",
        sa.Column(
            "root_execution_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment=(
                "Top of this execution's lineage chain; NULL until lineage is recorded"
            ),
        ),
    )
    op.add_column(
        "flow_execution",
        sa.Column(
            "delegation_depth",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment=(
                "Hops from root_execution_id; 0 for a root and for rows that "
                "predate lineage"
            ),
        ),
    )
    # Indexed because the whole point of the pair is "children of X" and
    # "everything in this chain": a subtree walk is one indexed lookup per
    # level, never a scan.
    op.create_foreign_key(
        "fk_flow_execution_parent_execution_id",
        "flow_execution",
        "flow_execution",
        ["parent_execution_id"],
        ["id"],
    )
    op.create_index(
        "ix_flow_execution_parent_execution_id",
        "flow_execution",
        ["parent_execution_id"],
        unique=False,
    )
    op.create_index(
        "ix_flow_execution_root_execution_id",
        "flow_execution",
        ["root_execution_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the lineage columns and their access paths; no other column moved."""
    op.drop_index("ix_flow_execution_root_execution_id", table_name="flow_execution")
    op.drop_index("ix_flow_execution_parent_execution_id", table_name="flow_execution")
    op.drop_constraint(
        "fk_flow_execution_parent_execution_id",
        "flow_execution",
        type_="foreignkey",
    )
    op.drop_column("flow_execution", "delegation_depth")
    op.drop_column("flow_execution", "root_execution_id")
    op.drop_column("flow_execution", "parent_execution_id")

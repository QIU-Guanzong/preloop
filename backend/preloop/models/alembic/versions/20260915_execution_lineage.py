"""Add execution lineage columns (parent, root, depth) to flow_execution.

Columns only: nothing writes a non-default value yet, so this revision lands
the tree shape without changing any existing behaviour.

Column shapes (a copy of the shipped ``retry_of_execution_id`` pattern in the
same table):

- ``parent_execution_id`` UUID, NULL, FK -> ``flow_execution.id`` (SET NULL on
  delete), indexed. The execution that started this one.
- ``root_execution_id`` UUID, NULL, indexed. The first execution of this
  lineage; NULL on the root itself.
- ``delegation_depth`` integer, NOT NULL, default 0. Distance from the root.

Backfill rule for rows that exist before this revision: ``delegation_depth``
is 0 and both id columns are NULL. Such a row is treated as its own root, the
same way a freshly created lineage-less execution is. The ``server_default``
on ``delegation_depth`` is what backfills existing rows; it is kept afterwards
so an INSERT that omits the column still lands as a root.

Revision ID: 20260915_execution_lineage
Revises: 20260914_pricing_merge
Create Date: 2026-09-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260915_execution_lineage"
down_revision: Union[str, None] = "20260914_pricing_merge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the three lineage columns, their foreign key and their indexes."""
    op.add_column(
        "flow_execution",
        sa.Column(
            "parent_execution_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_flow_execution_parent_execution_id",
        "flow_execution",
        "flow_execution",
        ["parent_execution_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_flow_execution_parent_execution_id"),
        "flow_execution",
        ["parent_execution_id"],
        unique=False,
    )
    op.add_column(
        "flow_execution",
        sa.Column(
            "root_execution_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_index(
        op.f("ix_flow_execution_root_execution_id"),
        "flow_execution",
        ["root_execution_id"],
        unique=False,
    )
    op.add_column(
        "flow_execution",
        sa.Column(
            "delegation_depth",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    """Drop the lineage columns and everything added for them."""
    op.drop_column("flow_execution", "delegation_depth")
    op.drop_index(
        op.f("ix_flow_execution_root_execution_id"),
        table_name="flow_execution",
    )
    op.drop_column("flow_execution", "root_execution_id")
    op.drop_index(
        op.f("ix_flow_execution_parent_execution_id"),
        table_name="flow_execution",
    )
    op.drop_constraint(
        "fk_flow_execution_parent_execution_id",
        "flow_execution",
        type_="foreignkey",
    )
    op.drop_column("flow_execution", "parent_execution_id")

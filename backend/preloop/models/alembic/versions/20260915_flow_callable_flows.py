"""Add the ``callable_flows`` delegation allowlist column to ``flow``.

Column only: nothing enforces the allowlist yet, so this revision lands the
shape without changing any existing behaviour.

Shape (a copy of the shipped ``allowed_mcp_tools`` pattern on the same table):
``callable_flows`` JSON, NULL, default NULL. A list of objects, each naming a
flow inside the owning account plus optional per child ceilings. NULL and ``[]``
mean the same thing, no delegation, so existing rows are already fail closed
and need no backfill.

The list lives on the flow row rather than in the api key context an execution
carries, because that context is minted at launch: an allowlist read from it
could not be revoked while a run is in flight.

Revision ID: 20260915_flow_callable_flows
Revises: 20260915_execution_lineage
Create Date: 2026-09-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260915_flow_callable_flows"
down_revision: Union[str, None] = "20260915_execution_lineage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the nullable ``callable_flows`` JSON column."""
    op.add_column(
        "flow",
        sa.Column("callable_flows", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    """Drop the ``callable_flows`` column."""
    op.drop_column("flow", "callable_flows")

"""Parent, root and delegation-depth lineage columns on flow_execution.

A run derived from another run of the same flow (a retry, the resume of a
parked run, a CI-failure or PR-comment resume, a durable-feedback repair turn)
previously left no single link to that run: ``retry_of_execution_id`` covers
retries only, and the parked parent's ``resume_execution_id`` points at the
child rather than the parent. These three columns record the general link
(``parent_execution_id``), the first run of the chain (``root_execution_id``)
and the chain distance (``delegation_depth``).

The columns land before their writer, so nothing sets a non-default value yet:
every row, old or new, reads ``parent_execution_id`` null,
``root_execution_id`` null and ``delegation_depth`` 0. That is also the
backfill rule for rows that predate the columns, and no data migration is
needed to reach it - ``delegation_depth`` is added NOT NULL DEFAULT 0, so
Postgres fills existing rows from the default in the same statement. Both id
columns stay NULL. Parentage is deliberately not reconstructed from
``retry_of_execution_id``: only the run_flow continuation knows which run
started which, and a guessed chain is worse than an honest "not recorded".

``parent_execution_id`` is ON DELETE SET NULL: deleting a flow cascades to its
executions as one batch of DELETEs in load order, so a root older than the
child pointing at it would be removed while the child still references it,
which a plain self-reference refuses. ``root_execution_id`` is indexed for the
chain listing but carries no constraint of its own.

Revision ID: 20260915_flow_execution_lineage
Revises: 20260914_pricing_merge
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260915_flow_execution_lineage"
down_revision: Union[str, None] = "20260914_pricing_merge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the two nullable id columns, the depth and their indexes."""
    # Adding a nullable column with no default is metadata-only in Postgres.
    op.add_column(
        "flow_execution",
        sa.Column("parent_execution_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "flow_execution",
        sa.Column("root_execution_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # NOT NULL DEFAULT 0 is what backfills rows that predate the column; they
    # are the head of their own chain until a writer says otherwise.
    op.add_column(
        "flow_execution",
        sa.Column(
            "delegation_depth",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    # Both id columns are looked up directly - listing a chain filters on its
    # root and walking up from a run reads its parent - and both are written
    # once at INSERT, so plain b-tree indexes are enough.
    op.create_index(
        "ix_flow_execution_parent_execution_id",
        "flow_execution",
        ["parent_execution_id"],
    )
    op.create_index(
        "ix_flow_execution_root_execution_id",
        "flow_execution",
        ["root_execution_id"],
    )
    # ON DELETE SET NULL, unlike the plain self-references on
    # retry_of_execution_id and resume_execution_id. A flow delete cascades to
    # every execution of that flow as one batch of DELETEs in load order, so a
    # root older than the child pointing at it would be removed first, which a
    # plain self-FK refuses. The link is derived data: a surviving row keeps
    # NULL ("not recorded") rather than failing the delete.
    op.create_foreign_key(
        "fk_flow_execution_parent_execution_id",
        "flow_execution",
        "flow_execution",
        ["parent_execution_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Drop the lineage columns; the links are not kept elsewhere."""
    op.drop_constraint(
        "fk_flow_execution_parent_execution_id",
        "flow_execution",
        type_="foreignkey",
    )
    op.drop_index("ix_flow_execution_root_execution_id", table_name="flow_execution")
    op.drop_index("ix_flow_execution_parent_execution_id", table_name="flow_execution")
    op.drop_column("flow_execution", "delegation_depth")
    op.drop_column("flow_execution", "root_execution_id")
    op.drop_column("flow_execution", "parent_execution_id")

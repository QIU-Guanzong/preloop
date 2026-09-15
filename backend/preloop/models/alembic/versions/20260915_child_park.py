"""Park kind on flow_execution, so a run can park on its children.

A parent that delegated work parks the same way a run waiting for a human
does, and the orchestrator has to tell the two apart while the row is still
RUNNING, before either WAITING_FOR_HUMAN or WAITING_FOR_CHILDREN is written.
One small column carries that, plus the partial index the child wait deadline
sweep reads.

Revision ID: 20260915_child_park
Revises: 20260915_flow_callable_flows
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_child_park"
down_revision: Union[str, None] = "20260915_flow_callable_flows"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add park_kind and the deadline index for parents parked on children."""
    op.add_column(
        "flow_execution",
        sa.Column("park_kind", sa.String(length=16), nullable=True),
    )
    # Every row that exists today is parked on a human, because that is the
    # only park there was. Backfilling the parked ones (rather than all rows)
    # keeps the column meaning "what this run is parked on", not "what it
    # would park on".
    op.execute(
        "UPDATE flow_execution SET park_kind = 'human' "
        "WHERE park_request_id IS NOT NULL"
    )
    # The child wait sweep scans parents whose deadline has passed, the same
    # shape as the approval expiry index next to it.
    op.create_index(
        "ix_flow_execution_child_park_expires_at",
        "flow_execution",
        ["park_expires_at"],
        postgresql_where=sa.text("status = 'WAITING_FOR_CHILDREN'"),
    )


def downgrade() -> None:
    """Drop the park kind. Parents parked on children stay parked."""
    op.drop_index(
        "ix_flow_execution_child_park_expires_at", table_name="flow_execution"
    )
    op.drop_column("flow_execution", "park_kind")

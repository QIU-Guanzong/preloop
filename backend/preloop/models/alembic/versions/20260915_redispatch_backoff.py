"""How often the stale-claim reaper has already re-published an execution.

Two columns on flow_execution. The recovery loop used to keep this in a
process-local dict, which bounded one worker and nothing else: with N
replicas an execution nobody can claim was still published N times per
interval, forever. Persisting the attempt count and the last publish time
moves the backoff into the one place every replica shares.

Revision ID: 20260915_redispatch_backoff
Revises: 20260915_child_park
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_redispatch_backoff"
down_revision: Union[str, None] = "20260915_child_park"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add the re-dispatch attempt counter and its timestamp."""
    op.add_column(
        "flow_execution",
        sa.Column(
            "redispatch_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "flow_execution",
        sa.Column(
            "last_redispatch_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Drop the re-dispatch backoff columns."""
    op.drop_column("flow_execution", "last_redispatch_at")
    op.drop_column("flow_execution", "redispatch_count")

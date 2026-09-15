"""Why a PENDING flow execution has not been admitted yet.

One nullable column. The per-account concurrency cap refuses admission at
claim time and leaves the execution PENDING; without a field on the row the
only record of that decision is a worker log line, and the recovery loop
revisits every unclaimed execution every 30 seconds, so logging it is not an
option either.

Revision ID: 20260915_queued_reason
Revises: 20260914_pricing_merge
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_queued_reason"
down_revision: Union[str, None] = "20260914_pricing_merge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add flow_execution.queued_reason."""
    op.add_column(
        "flow_execution",
        sa.Column("queued_reason", sa.String(length=200), nullable=True),
    )


def downgrade() -> None:
    """Drop flow_execution.queued_reason."""
    op.drop_column("flow_execution", "queued_reason")

"""Join queued_reason and execution lineage Alembic heads.

Both revisions parented on 20260914_pricing_merge. Keep both published
chains valid for databases already on either branch.

Revision ID: 20260915_queued_lineage_merge
Revises: 20260915_queued_reason, 20260915_execution_lineage
"""

from typing import Sequence, Union

revision: str = "20260915_queued_lineage_merge"
down_revision: Union[str, tuple[str, ...], None] = (
    "20260915_queued_reason",
    "20260915_execution_lineage",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Join the heads after both prerequisite chains have run."""


def downgrade() -> None:
    """Unjoin the heads without changing either prerequisite chain."""

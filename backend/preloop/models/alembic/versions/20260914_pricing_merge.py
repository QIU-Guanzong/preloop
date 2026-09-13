"""Join durable pricing and current control/repricing migrations.

Keep both published revision chains valid for databases already on either branch.
"""

revision = "20260914_pricing_merge"
down_revision = ("20260913_repricing_job", "20260912_hosted_spend")
branch_labels = None
depends_on = None
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Join the heads after both prerequisite chains have run."""


def downgrade() -> None:
    """Unjoin the heads without changing either prerequisite chain."""

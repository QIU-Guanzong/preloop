"""Mark a runner row as belonging to one process.

``preloop runner fg --once --ephemeral`` registers a runner for the lifetime
of a single CI job. A job that is cancelled hard never gets to unregister, and
without this flag the row stays offline forever and shows up as a phantom on
the Runners page. One boolean lets the offline sweep tell "this machine is
temporarily unreachable" apart from "this process is gone", so only the second
case is deleted.

Revision ID: 20260916_ephemeral_runner
Revises: 20260915_session_backfill
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260916_ephemeral_runner"
down_revision: Union[str, None] = "20260915_session_backfill"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add flow_runner.ephemeral."""
    op.add_column(
        "flow_runner",
        sa.Column(
            "ephemeral",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_index(
        "ix_flow_runner_ephemeral", "flow_runner", ["ephemeral"], unique=False
    )


def downgrade() -> None:
    """Drop flow_runner.ephemeral."""
    op.drop_index("ix_flow_runner_ephemeral", table_name="flow_runner")
    op.drop_column("flow_runner", "ephemeral")

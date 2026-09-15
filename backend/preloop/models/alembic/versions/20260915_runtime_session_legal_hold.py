"""Add the legal hold enforcement flag to runtime_session.

Holds covered executions, approvals and evidence packs. A runtime session had
no flag, so the retention purge deleted sessions and their activity rows by
cutoff alone and a hold could not reach them (issue #650).

This adds the same column shape the other three held tables carry: a NOT NULL
boolean defaulting to false, with a partial index over the held rows because
the purge only ever asks "is this one held?" and held rows are the rare case.
Existing rows land as not held, which is the state they were already in.

Activity rows need no flag of their own: ``runtime_session_activity`` is tied
to its session with ``ON DELETE CASCADE``, so a session the purge never
touches keeps its activity.

Revision ID: 20260915_session_hold
Revises: 20260915_queued_lineage_merge
Create Date: 2026-09-15
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260915_session_hold"
down_revision: Union[str, None] = "20260915_queued_lineage_merge"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

_TABLE = "runtime_session"
_INDEX = "ix_runtime_session_legal_hold"


def upgrade() -> None:
    """Add the flag and its partial index."""
    op.add_column(
        _TABLE,
        sa.Column(
            "legal_hold",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="True while a legal hold blocks this session from purge",
        ),
    )
    op.create_index(
        _INDEX,
        _TABLE,
        ["legal_hold"],
        postgresql_where=sa.text("legal_hold"),
    )


def downgrade() -> None:
    """Drop the flag. Sessions a purge already removed do not come back."""
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "legal_hold")

"""Record the managed agent that authored an operator note.

``agent_control_command.created_by_user_id`` names a human, and a note written
by an agent has no human behind it. Storing the author agent in its own column
(rather than only inside the envelope) is what lets the per author rate limit
key on the agent with an indexed query, and what keeps the record honest when
the agent is later renamed or removed.

Revision ID: 20260915_agent_note_author
Revises: 20260915_session_hold
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260915_agent_note_author"
down_revision: Union[str, None] = "20260915_session_hold"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add agent_control_command.created_by_managed_agent_id and its index."""
    op.add_column(
        "agent_control_command",
        sa.Column(
            "created_by_managed_agent_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_agent_control_command_author_agent",
        "agent_control_command",
        "managed_agent",
        ["created_by_managed_agent_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # The rate limit asks "how many notes has this agent written in the last
    # hour", so the author column is the leading key. Partial on notes: no
    # control envelope ever carries an agent author.
    op.create_index(
        "ix_agent_control_note_author_agent",
        "agent_control_command",
        ["created_by_managed_agent_id", "created_at"],
        postgresql_where=sa.text("kind = 'note'"),
    )


def downgrade() -> None:
    """Drop the author agent column, its index and its foreign key."""
    op.drop_index(
        "ix_agent_control_note_author_agent", table_name="agent_control_command"
    )
    op.drop_constraint(
        "fk_agent_control_command_author_agent",
        "agent_control_command",
        type_="foreignkey",
    )
    op.drop_column("agent_control_command", "created_by_managed_agent_id")

"""Record when a user answered the post-signup trial offer.

The offer ("start your trial" or "continue on Free") is shown once per person.
A browser-local flag would show it again on the next device and after every
cache clear, so the answer is stored on the user. Null means unanswered, which
is what every existing row is: nobody has been asked yet.

Revision ID: 20260916_trial_prompt
Revises: 20260916_runner_concurrency
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260916_trial_prompt"
down_revision: Union[str, None] = "20260916_runner_concurrency"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add user.trial_prompt_dismissed_at."""
    op.add_column(
        "user",
        sa.Column(
            "trial_prompt_dismissed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the user answered the post-signup trial offer",
        ),
    )


def downgrade() -> None:
    """Drop user.trial_prompt_dismissed_at."""
    op.drop_column("user", "trial_prompt_dismissed_at")

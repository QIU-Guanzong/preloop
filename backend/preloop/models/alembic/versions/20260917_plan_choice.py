"""Rename the trial-prompt dismissal into an honest plan-choice stamp.

The column used to record "this person closed the trial dialog". The dialog
is gone: what the product now records is "this person chose a plan", whether
they chose it on the pricing page before signing up, on the first-login plan
choice, or by completing a checkout. The old name described a widget, the new
one describes the fact, so the column is renamed rather than replaced and
every recorded answer survives the rename.

Existing users are then backfilled to "chosen". Anyone who already has an
account has been using the product without ever being asked, and a release is
not a reason to interrupt them with a full-screen question. The backfill is
what implements the rule, deliberately, instead of a "created before this
shipped" clause in the runtime check that nobody could ever delete.

Revision ID: 20260917_plan_choice
Revises: 20260917_onboarding_claim
"""

from typing import Sequence, Union

from alembic import op

revision: str = "20260917_plan_choice"
down_revision: Union[str, None] = "20260917_onboarding_claim"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Rename the column, then stamp every user that already exists."""
    op.alter_column(
        "user",
        "trial_prompt_dismissed_at",
        new_column_name="plan_choice_made_at",
        comment="When this user chose a plan (null while the choice is open)",
        existing_comment="When the user answered the post-signup trial offer",
    )
    # The existing-user rule, in one statement. created_at is the honest
    # timestamp for "they were here before the question existed"; the
    # coalesce only covers rows old enough to predate that column being
    # populated, which no current row is.
    op.execute(
        """
        UPDATE "user"
        SET plan_choice_made_at = COALESCE(created_at, NOW())
        WHERE plan_choice_made_at IS NULL
        """
    )


def downgrade() -> None:
    """Rename back.

    The backfill is not undone: there is no column recording which rows it
    touched, and guessing would un-answer people who really did answer. The
    visible consequence of a downgrade is that the old dialog treats every
    pre-existing user as having dismissed it, which is what it did for them
    anyway.
    """
    op.alter_column(
        "user",
        "plan_choice_made_at",
        new_column_name="trial_prompt_dismissed_at",
        comment="When the user answered the post-signup trial offer",
        existing_comment="When this user chose a plan (null while the choice is open)",
    )

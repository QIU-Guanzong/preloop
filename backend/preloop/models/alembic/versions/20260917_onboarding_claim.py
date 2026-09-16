"""Store the fingerprint of an outstanding onboarding claim token.

An account created by a completed checkout has no password yet. The welcome
page sets the first one, and until now the only thing standing between an
anonymous caller and that account was knowing the address. The claim token
minted at checkout success is the real credential; this column holds its
SHA-256 so the server can verify it once and then spend it. Null, the value
every existing row gets, means no claim is outstanding.

Revision ID: 20260917_onboarding_claim
Revises: 20260916_trial_prompt
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260917_onboarding_claim"
down_revision: Union[str, None] = "20260916_trial_prompt"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Add user.onboarding_claim_hash."""
    op.add_column(
        "user",
        sa.Column(
            "onboarding_claim_hash",
            sa.String(length=64),
            nullable=True,
            comment="SHA-256 of the outstanding single-use onboarding claim token",
        ),
    )


def downgrade() -> None:
    """Drop user.onboarding_claim_hash."""
    op.drop_column("user", "onboarding_claim_hash")

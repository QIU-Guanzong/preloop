"""Give the session search corpus vectors, and accounts a way to opt in.

Three things land together because they are useless apart:

1. ``session_search_document`` gains ``embedding``, the model identity that
   produced it, when it was produced and how many provider attempts it has
   cost. The identity column is what makes a later dimension or provider
   change survivable: every stored vector says what made it, so a sweep can
   find exactly the rows it has to redo instead of truncating the corpus.
2. An HNSW index over ``embedding`` restricted to rows that have one. The
   corpus is written long before it is embedded and an account may never opt
   in at all, so the overwhelming majority of rows are NULL; a full index
   would cost the whole table to index nothing. ``vector_cosine_ops`` matches
   the normalized vectors every supported provider returns.
3. ``session_embedding_setting``, one row per account: off by default, and
   naming the provider and model the account's text is about to be sent to.
   This is an account decision, not a deployment one, which is why it is a
   table and not another entry in ``config.py``.

Dimensions are 1536 (issue #624 open decision 1). The number is a decision,
not a measurement: it is what the OpenAI compatible defaults return, and the
storage difference against 512 is roughly threefold. A vector column and an
HNSW index both need a fixed width, so changing it later is a migration.

Revision ID: 20260915_session_embedding
Revises: 20260915_session_search
Create Date: 2026-09-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "20260915_session_embedding"
down_revision: Union[str, None] = "20260915_session_search"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"

EMBEDDING_DIMENSIONS = 1536


def upgrade() -> None:
    """Add the vector columns, the vector index and the opt in table."""
    # pgvector is already a dependency of the shipped gateway corpus, but the
    # extension is created defensively so a database that never carried a
    # vector column can still run this.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.add_column(
        "session_search_document",
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
    )
    op.add_column(
        "session_search_document",
        sa.Column("embedding_model", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "session_search_document",
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "session_search_document",
        sa.Column(
            "embedding_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.execute(
        "CREATE INDEX ix_session_search_document_embedding "
        "ON session_search_document USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64) "
        "WHERE embedding IS NOT NULL"
    )

    op.create_table(
        "session_embedding_setting",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "provider",
            sa.String(length=32),
            nullable=False,
            server_default="openai_compatible",
        ),
        sa.Column("base_url", sa.String(length=500), nullable=True),
        sa.Column("model_identifier", sa.String(length=255), nullable=True),
        sa.Column(
            "dimensions",
            sa.Integer(),
            nullable=False,
            server_default=str(EMBEDDING_DIMENSIONS),
        ),
        sa.Column("daily_cap_usd", sa.Float(), nullable=True),
        sa.Column("degraded_reason", sa.String(length=64), nullable=True),
        sa.Column("degraded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["enabled_by_user_id"], ["user.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "ix_session_embedding_setting_id", "session_embedding_setting", ["id"]
    )
    # One row per account, enforced by the index that also serves the lookup.
    op.create_index(
        "ix_session_embedding_setting_account_id",
        "session_embedding_setting",
        ["account_id"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the opt in table, the vector index and the vector columns."""
    op.drop_index(
        "ix_session_embedding_setting_account_id",
        table_name="session_embedding_setting",
    )
    op.drop_index(
        "ix_session_embedding_setting_id", table_name="session_embedding_setting"
    )
    op.drop_table("session_embedding_setting")
    op.execute("DROP INDEX IF EXISTS ix_session_search_document_embedding")
    op.drop_column("session_search_document", "embedding_attempts")
    op.drop_column("session_search_document", "embedded_at")
    op.drop_column("session_search_document", "embedding_model")
    op.drop_column("session_search_document", "embedding")

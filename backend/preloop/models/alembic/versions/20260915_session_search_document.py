"""Add the chunked runtime session search corpus.

One row is one chunk of one source row that already exists: a gateway
interaction, a transcript message, a tool call, an operator note, a session
summary or a flow log excerpt. The table is account scoped and session scoped
so a query is bounded by account before it reaches the full text index, and
both foreign keys cascade so deleting a session or an account takes its
chunks with it.

``search_vector`` is a STORED generated column, not an expression index over
the text. The shipped gateway corpus indexes ``to_tsvector('simple',
searchable_text)`` as an expression, which means ranking and snippet
generation recompute the vector for every candidate row at query time. A
stored vector is written once per chunk and read back, which is what a
ranked search over many small chunks needs. The cost is disk: the vector is
persisted next to the text.

Indexes created here:

- GIN on ``search_vector`` for the match itself.
- btree ``(account_id, occurred_at DESC)`` for the account scoped recent view.
- btree ``(runtime_session_id, occurred_at)`` for one session's timeline.
- unique ``(source_kind, source_id, chunk_index)`` so re-indexing one source
  can only replace its own chunks.
- partial btree on ``occurred_at`` where ``embedding_state = 'pending'`` so a
  later embedding worker can find its backlog without scanning the corpus.

Revision ID: 20260915_session_search
Revises: 20260915_redispatch_backoff
Create Date: 2026-09-15
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "20260915_session_search"
down_revision: Union[str, None] = "20260915_redispatch_backoff"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None
# Alembic reads these module globals by name; keep a local reference so static
# analysis treats them as used.
_ALEMBIC_IDENTIFIERS = (revision, down_revision, branch_labels, depends_on)
assert _ALEMBIC_IDENTIFIERS, "Alembic revision metadata must be defined"


def upgrade() -> None:
    """Create the corpus table, its generated vector and its indexes."""
    op.create_table(
        "session_search_document",
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
        sa.Column("runtime_session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_kind", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=255), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=True),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', content)", persisted=True),
            nullable=True,
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "redaction_state",
            sa.String(length=32),
            nullable=False,
            server_default="clear",
        ),
        sa.Column(
            "embedding_state",
            sa.String(length=32),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("model_alias", sa.String(length=255), nullable=True),
        sa.Column("provider_name", sa.String(length=128), nullable=True),
        sa.Column("runtime_principal_id", sa.String(length=255), nullable=True),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("flow_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=True),
        sa.Column("meta_data", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["account_id"], ["account.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["runtime_session_id"], ["runtime_session.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "source_kind",
            "source_id",
            "chunk_index",
            name="uq_session_search_document_source_chunk",
        ),
    )
    op.create_index(
        op.f("ix_session_search_document_id"),
        "session_search_document",
        ["id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_session_search_document_account_id"),
        "session_search_document",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_session_search_document_runtime_session_id"),
        "session_search_document",
        ["runtime_session_id"],
        unique=False,
    )
    op.create_index(
        "ix_session_search_document_vector",
        "session_search_document",
        ["search_vector"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_session_search_document_account_time",
        "session_search_document",
        ["account_id", "occurred_at"],
        unique=False,
        postgresql_ops={"occurred_at": "DESC"},
    )
    op.create_index(
        "ix_session_search_document_session_time",
        "session_search_document",
        ["runtime_session_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_session_search_document_embedding_pending",
        "session_search_document",
        ["occurred_at"],
        unique=False,
        postgresql_where=sa.text("embedding_state = 'pending'"),
    )


def downgrade() -> None:
    """Drop the corpus table and everything created with it."""
    op.drop_index(
        "ix_session_search_document_embedding_pending",
        table_name="session_search_document",
    )
    op.drop_index(
        "ix_session_search_document_session_time",
        table_name="session_search_document",
    )
    op.drop_index(
        "ix_session_search_document_account_time",
        table_name="session_search_document",
    )
    op.drop_index(
        "ix_session_search_document_vector",
        table_name="session_search_document",
    )
    op.drop_index(
        op.f("ix_session_search_document_runtime_session_id"),
        table_name="session_search_document",
    )
    op.drop_index(
        op.f("ix_session_search_document_account_id"),
        table_name="session_search_document",
    )
    op.drop_index(
        op.f("ix_session_search_document_id"),
        table_name="session_search_document",
    )
    op.drop_table("session_search_document")

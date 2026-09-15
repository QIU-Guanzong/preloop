"""Per account opt in for embedding session content.

Embedding session chunks sends the account's own agent text to a provider.
That is a decision the account makes, not one a deployment makes on its
behalf, so the switch lives here rather than in deployment config: one row
per account, absent or ``enabled = False`` until somebody turns it on, and
enabling requires naming the provider and the model the text is about to be
sent to.

The deployment keeps a kill switch of its own
(``SESSION_EMBEDDING_ENABLED``). Both must say yes. The kill switch stops
embedding only: keyword indexing into the corpus is a separate setting and
keeps running.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base
from .session_search_document import EMBEDDING_DIMENSIONS

if TYPE_CHECKING:
    from .account import Account

#: An OpenAI compatible ``/embeddings`` endpoint named by base url. This is
#: the provider a self hosted or air gapped install actually has: the vectors
#: are produced by whatever the operator runs, and the text never leaves it.
PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"
#: A sentence-transformers model loaded in process, which is the path
#: ``crud/embedding.py`` already supports for issue embeddings.
PROVIDER_LOCAL = "local"

EMBEDDING_PROVIDERS = (PROVIDER_OPENAI_COMPATIBLE, PROVIDER_LOCAL)

#: Reason codes recorded on the row when a run could not do its work. These
#: are degraded states, not errors: the chunks stay pending and the next run
#: picks them up.
DEGRADED_DAILY_CAP = "daily_cap_reached"
DEGRADED_PROVIDER_ERROR = "provider_error"
DEGRADED_DIMENSION_MISMATCH = "dimension_mismatch"
DEGRADED_MISCONFIGURED = "misconfigured"

DEGRADED_REASONS = (
    DEGRADED_DAILY_CAP,
    DEGRADED_PROVIDER_ERROR,
    DEGRADED_DIMENSION_MISMATCH,
    DEGRADED_MISCONFIGURED,
)


class SessionEmbeddingSetting(Base):
    """One account's answer to "may we embed this account's session text"."""

    __tablename__ = "session_embedding_setting"

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("account.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    provider: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=PROVIDER_OPENAI_COMPATIBLE,
        server_default=PROVIDER_OPENAI_COMPATIBLE,
    )
    #: Required for :data:`PROVIDER_OPENAI_COMPATIBLE`. The whole point of the
    #: opt in is that this value is visible: enabling names the endpoint the
    #: account's text is about to be posted to.
    base_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    model_identifier: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    dimensions: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=EMBEDDING_DIMENSIONS,
        server_default=str(EMBEDDING_DIMENSIONS),
    )
    #: Per account daily spend ceiling in USD. NULL falls back to the
    #: deployment default.
    daily_cap_usd: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    degraded_reason: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    degraded_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    enabled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    enabled_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("user.id", ondelete="SET NULL"),
        nullable=True,
    )

    account: Mapped["Account"] = relationship("Account")

    @property
    def model_identity(self) -> Optional[str]:
        """Stable identity stamped on every vector this setting produces."""
        if not self.model_identifier:
            return None
        return f"{self.provider}:{self.model_identifier}@{self.dimensions}"

    def __repr__(self) -> str:
        return (
            f"<SessionEmbeddingSetting(account_id={self.account_id}, "
            f"enabled={self.enabled}, provider={self.provider})>"
        )

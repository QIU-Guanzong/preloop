"""CRUD helpers for the chunked runtime session search corpus.

The corpus is written per source: every write replaces the chunks of exactly
one source row (one gateway interaction, one transcript message, one tool
call, one operator note, one summary, one log excerpt) and touches nothing
else. Writes are idempotent on the content hash, so re-indexing an unchanged
source is a no op rather than a delete and insert.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models.session_search_document import (
    EMBEDDING_STATE_PENDING,
    REDACTION_STATE_CLEAR,
    SessionSearchDocument,
)
from .base import CRUDBase


def content_hash_for(text: str) -> str:
    """Return the stable content hash used to detect an unchanged chunk."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class SessionSearchChunk:
    """One chunk offered to the corpus by a writer."""

    content: str
    chunk_index: int = 0
    role: Optional[str] = None
    redaction_state: str = REDACTION_STATE_CLEAR
    embedding_state: str = EMBEDDING_STATE_PENDING
    model_alias: Optional[str] = None
    provider_name: Optional[str] = None
    runtime_principal_id: Optional[str] = None
    api_key_id: Optional[Any] = None
    flow_id: Optional[Any] = None
    status: Optional[str] = None
    meta_data: Optional[Dict[str, Any]] = field(default=None)


class CRUDSessionSearchDocument(CRUDBase[SessionSearchDocument]):
    """CRUD operations for `SessionSearchDocument`."""

    def list_for_source(
        self, db: Session, *, source_kind: str, source_id: str
    ) -> List[SessionSearchDocument]:
        """Return every stored chunk of one source, in chunk order."""
        return (
            db.query(SessionSearchDocument)
            .filter(
                SessionSearchDocument.source_kind == source_kind,
                SessionSearchDocument.source_id == str(source_id),
            )
            .order_by(SessionSearchDocument.chunk_index.asc())
            .all()
        )

    def delete_for_source(
        self, db: Session, *, source_kind: str, source_id: str
    ) -> int:
        """Delete every chunk of one source and return how many went."""
        deleted = (
            db.query(SessionSearchDocument)
            .filter(
                SessionSearchDocument.source_kind == source_kind,
                SessionSearchDocument.source_id == str(source_id),
            )
            .delete(synchronize_session=False)
        )
        db.flush()
        return int(deleted or 0)

    def replace_source_chunks(
        self,
        db: Session,
        *,
        account_id: Any,
        runtime_session_id: Any,
        source_kind: str,
        source_id: str,
        occurred_at: datetime,
        chunks: Sequence[SessionSearchChunk],
        commit: bool = False,
    ) -> List[SessionSearchDocument]:
        """Store the chunks of one source, skipping an unchanged rewrite.

        The stored chunks are compared with the offered ones by content hash
        and position. An identical set is left alone (no delete, no insert,
        no changed row count); anything else replaces the source's chunks
        wholesale, which is what keeps a shrinking source from leaving
        orphans behind.
        """
        existing = self.list_for_source(
            db, source_kind=source_kind, source_id=str(source_id)
        )
        new_hashes = [content_hash_for(chunk.content) for chunk in chunks]
        if [row.content_hash for row in existing] == new_hashes and len(
            existing
        ) == len(chunks):
            return existing

        if existing:
            self.delete_for_source(
                db, source_kind=source_kind, source_id=str(source_id)
            )

        stored: List[SessionSearchDocument] = []
        for chunk, chunk_hash in zip(chunks, new_hashes, strict=False):
            db_obj = SessionSearchDocument(
                account_id=account_id,
                runtime_session_id=runtime_session_id,
                source_kind=source_kind,
                source_id=str(source_id),
                chunk_index=chunk.chunk_index,
                occurred_at=occurred_at,
                role=chunk.role,
                content=chunk.content,
                content_hash=chunk_hash,
                redaction_state=chunk.redaction_state,
                embedding_state=chunk.embedding_state,
                model_alias=chunk.model_alias,
                provider_name=chunk.provider_name,
                runtime_principal_id=chunk.runtime_principal_id,
                api_key_id=chunk.api_key_id,
                flow_id=chunk.flow_id,
                status=chunk.status,
                meta_data=chunk.meta_data,
            )
            db.add(db_obj)
            stored.append(db_obj)

        db.flush()
        if commit:
            db.commit()
            for db_obj in stored:
                db.refresh(db_obj)
        return stored

    def search_account_chunks(
        self,
        db: Session,
        *,
        account_id: Any,
        query: Optional[str] = None,
        runtime_session_id: Optional[Any] = None,
        source_kind: Optional[str] = None,
        role: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[SessionSearchDocument]:
        """Return account scoped chunks, newest first, optionally matched.

        The account filter is applied to the corpus table itself rather than
        to a joined source row, so a session id belonging to another account
        matches nothing whatever else is passed.
        """
        stmt = db.query(SessionSearchDocument).filter(
            SessionSearchDocument.account_id == account_id
        )
        if runtime_session_id is not None:
            stmt = stmt.filter(
                SessionSearchDocument.runtime_session_id == runtime_session_id
            )
        if source_kind:
            stmt = stmt.filter(SessionSearchDocument.source_kind == source_kind)
        if role:
            stmt = stmt.filter(SessionSearchDocument.role == role)
        if start_date:
            stmt = stmt.filter(SessionSearchDocument.occurred_at >= start_date)
        if end_date:
            stmt = stmt.filter(SessionSearchDocument.occurred_at < end_date)

        normalized_query = " ".join(query.strip().split()) if query else None
        if normalized_query:
            stmt = stmt.filter(
                SessionSearchDocument.search_vector.op("@@")(
                    func.websearch_to_tsquery("simple", normalized_query)
                )
            )

        return (
            stmt.order_by(
                SessionSearchDocument.occurred_at.desc(),
                SessionSearchDocument.chunk_index.asc(),
            )
            .limit(limit)
            .offset(offset)
            .all()
        )

    def count_for_session(
        self, db: Session, *, account_id: Any, runtime_session_id: Any
    ) -> int:
        """Return how many chunks one session holds inside one account."""
        return int(
            db.query(func.count(SessionSearchDocument.id))
            .filter(
                SessionSearchDocument.account_id == account_id,
                SessionSearchDocument.runtime_session_id == runtime_session_id,
            )
            .scalar()
            or 0
        )

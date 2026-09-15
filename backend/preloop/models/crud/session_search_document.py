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
from datetime import UTC, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models.api_usage import ApiUsage
from ..models.flow import Flow
from ..models.flow_execution import FlowExecution
from ..models.session_search_document import (
    EMBEDDING_STATE_EMBEDDED,
    EMBEDDING_STATE_FAILED,
    EMBEDDING_STATE_IN_PROGRESS,
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

    # ------------------------------------------------------------------
    # Embedding queue
    #
    # The corpus is written by the request path and embedded by a worker.
    # Everything below is the worker's half: which chunks it is allowed to
    # take, how it takes them exactly once, and how a vector gets written
    # back with the identity of whatever produced it.
    # ------------------------------------------------------------------

    def held_runtime_session_ids(self, db: Session, *, account_id: Any) -> set[str]:
        """Sessions frozen by an active legal hold, for this account.

        The base this branch is stacked on has no hold flag on
        ``runtime_session``, so a session is treated as held when any of its
        metered gateway rows belongs to a flow execution under hold.
        Issue #650 has since added ``runtime_session.legal_hold`` on main:
        when this stack lands, that column belongs in the union here, and
        this comment is the pointer to do it.

        Evaluated as a set rather than a join in the claim query because the
        common case is an empty set, and an empty set costs one cheap index
        lookup instead of a correlated subquery per candidate chunk.
        """
        held_executions = select(FlowExecution.id).where(
            FlowExecution.legal_hold.is_(True),
            FlowExecution.flow_id.in_(
                select(Flow.id).where(Flow.account_id == account_id)
            ),
        )
        rows = db.execute(
            select(ApiUsage.runtime_session_id)
            .where(
                ApiUsage.account_id == account_id,
                ApiUsage.runtime_session_id.isnot(None),
                ApiUsage.flow_execution_id.in_(held_executions),
            )
            .distinct()
        ).scalars()
        return {str(value) for value in rows if value is not None}

    def _embeddable_filters(self, *, account_id: Any) -> list[Any]:
        """Conditions every embeddable chunk must satisfy.

        Only ``clear`` chunks qualify. A redacted chunk has had a credential
        masked and a metadata-only chunk never captured content at all;
        embedding either would put a vector of the mask, or of a descriptor,
        into a corpus that a semantic query then treats as the real thing.
        """
        return [
            SessionSearchDocument.account_id == account_id,
            SessionSearchDocument.embedding_state == EMBEDDING_STATE_PENDING,
            SessionSearchDocument.redaction_state == REDACTION_STATE_CLEAR,
            SessionSearchDocument.embedding.is_(None),
            SessionSearchDocument.content != "",
        ]

    def count_pending_embeddings(
        self,
        db: Session,
        *,
        account_id: Any,
        excluded_session_ids: Optional[Iterable[Any]] = None,
    ) -> int:
        """How many chunks this account still has waiting for a vector."""
        stmt = db.query(func.count(SessionSearchDocument.id)).filter(
            *self._embeddable_filters(account_id=account_id)
        )
        excluded = [str(value) for value in (excluded_session_ids or [])]
        if excluded:
            stmt = stmt.filter(
                SessionSearchDocument.runtime_session_id.notin_(excluded)
            )
        return int(stmt.scalar() or 0)

    def claim_pending_chunks(
        self,
        db: Session,
        *,
        account_id: Any,
        limit: int,
        excluded_session_ids: Optional[Iterable[Any]] = None,
        commit: bool = False,
    ) -> List[SessionSearchDocument]:
        """Claim the oldest waiting chunks of one session, oldest first.

        A batch never spans sessions. One batch produces one purpose tagged
        usage row, and a row that covered several sessions could not be
        attributed to any of them honestly; keeping the batch inside one
        session is what lets the spend be named and then excluded from that
        session's rollup.

        Claiming moves the rows to ``in_progress`` and commits, so a provider
        call that takes seconds does not hold row locks for its duration and
        a second worker skips these rows instead of waiting behind them.
        """
        if limit <= 0:
            return []
        excluded = [str(value) for value in (excluded_session_ids or [])]

        oldest_stmt = db.query(SessionSearchDocument.runtime_session_id).filter(
            *self._embeddable_filters(account_id=account_id)
        )
        if excluded:
            oldest_stmt = oldest_stmt.filter(
                SessionSearchDocument.runtime_session_id.notin_(excluded)
            )
        oldest = (
            oldest_stmt.order_by(
                SessionSearchDocument.occurred_at.asc(),
                SessionSearchDocument.chunk_index.asc(),
            )
            .limit(1)
            .first()
        )
        if oldest is None:
            return []
        runtime_session_id = oldest[0]

        claimed = (
            db.query(SessionSearchDocument)
            .filter(
                *self._embeddable_filters(account_id=account_id),
                SessionSearchDocument.runtime_session_id == runtime_session_id,
            )
            .order_by(
                SessionSearchDocument.occurred_at.asc(),
                SessionSearchDocument.chunk_index.asc(),
            )
            .limit(limit)
            .with_for_update(skip_locked=True)
            .all()
        )
        for row in claimed:
            row.embedding_state = EMBEDDING_STATE_IN_PROGRESS
            row.embedding_attempts = int(row.embedding_attempts or 0) + 1
        db.flush()
        if commit:
            db.commit()
            for row in claimed:
                db.refresh(row)
        return claimed

    def store_embeddings(
        self,
        db: Session,
        *,
        vectors: Sequence[Tuple[SessionSearchDocument, Sequence[float]]],
        model_identity: str,
        now: Optional[datetime] = None,
        commit: bool = False,
    ) -> int:
        """Write vectors back with the identity of the model that made them."""
        stamp = now or datetime.now(UTC)
        written = 0
        for row, vector in vectors:
            row.embedding = list(vector)
            row.embedding_model = model_identity
            row.embedded_at = stamp
            row.embedding_state = EMBEDDING_STATE_EMBEDDED
            written += 1
        db.flush()
        if commit:
            db.commit()
        return written

    def release_claim(
        self,
        db: Session,
        *,
        chunks: Sequence[SessionSearchDocument],
        max_attempts: int,
        commit: bool = False,
    ) -> int:
        """Return unembedded chunks to the queue, or retire the hopeless ones.

        A chunk the provider has already refused ``max_attempts`` times goes
        to ``failed`` instead of back to ``pending``: the alternative is one
        poison chunk at the head of the oldest-first queue starving every
        chunk behind it.
        """
        released = 0
        for row in chunks:
            if row.embedding_state != EMBEDDING_STATE_IN_PROGRESS:
                continue
            if int(row.embedding_attempts or 0) >= max_attempts:
                row.embedding_state = EMBEDDING_STATE_FAILED
            else:
                row.embedding_state = EMBEDDING_STATE_PENDING
            released += 1
        db.flush()
        if commit:
            db.commit()
        return released

    def count_embedded_for_account(self, db: Session, *, account_id: Any) -> int:
        """How many chunks of one account carry a vector."""
        return int(
            db.query(func.count(SessionSearchDocument.id))
            .filter(
                SessionSearchDocument.account_id == account_id,
                SessionSearchDocument.embedding.isnot(None),
            )
            .scalar()
            or 0
        )

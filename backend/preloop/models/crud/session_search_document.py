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

from sqlalchemy import String, case, delete, func, select, update
from sqlalchemy.orm import Session

from ..models.runtime_session import RuntimeSession
from ..models.session_search_document import (
    EMBEDDING_STATE_PENDING,
    REDACTION_STATE_CLEAR,
    REDACTION_STATE_WITHHELD,
    TEXT_RETURNABLE_REDACTION_STATES,
    SessionSearchDocument,
)
from .base import CRUDBase


def _held_session_exists() -> Any:
    """True when the chunk's session is under legal hold.

    Shared by the usage-purge delete and the usage-orphan count so the two
    cannot disagree about which chunks a hold is allowed to keep after the
    usage row they quote is gone.
    """
    return (
        select(RuntimeSession.id)
        .where(RuntimeSession.id == SessionSearchDocument.runtime_session_id)
        .where(RuntimeSession.legal_hold.is_(True))
        .exists()
    )


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


@dataclass
class SessionSearchHit:
    """One chunk as a search response may see it.

    The difference between this and the row is ``content``: a hit built by
    :meth:`CRUDSessionSearchDocument.search_account_hits` carries text only
    when the chunk's redaction state allows it, and the decision is made by
    the database in the projection rather than by the caller after the fact.
    A caller that forgets to check ``text_withheld`` still cannot leak, which
    is the only property that makes this safe to hand to an endpoint.
    """

    id: Any
    runtime_session_id: Any
    source_kind: str
    source_id: str
    chunk_index: int
    occurred_at: datetime
    role: Optional[str]
    content: str
    redaction_state: str
    text_withheld: bool
    model_alias: Optional[str] = None
    provider_name: Optional[str] = None
    flow_id: Optional[Any] = None
    status: Optional[str] = None
    meta_data: Optional[Dict[str, Any]] = None


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

    def delete_for_sources(
        self,
        db: Session,
        *,
        source_kind: str,
        source_ids: Sequence[Any],
        excluding_held_sessions: bool = False,
    ) -> int:
        """Delete every chunk of many sources of one kind in one statement.

        Used by the purge, which removes its rows in id batches and has to
        take the chunks quoting them in the same pass. An empty batch is a no
        op rather than an unbounded ``IN ()``.

        ``excluding_held_sessions`` keeps chunks whose session is under legal
        hold. The usage purge sets this so a hold outranks a cutoff on a
        different class; the same predicate is used by
        :meth:`count_orphans_for_sources`.
        """
        wanted = [str(value) for value in source_ids]
        if not wanted:
            return 0
        stmt = delete(SessionSearchDocument).where(
            SessionSearchDocument.source_kind == source_kind,
            SessionSearchDocument.source_id.in_(wanted),
        )
        if excluding_held_sessions:
            stmt = stmt.where(~_held_session_exists())
        result = db.execute(stmt.execution_options(synchronize_session=False))
        return int(result.rowcount or 0)

    def delete_for_sessions(
        self, db: Session, *, runtime_session_ids: Sequence[Any]
    ) -> int:
        """Delete every chunk of many sessions in one statement.

        The foreign key cascades, so the purge's ``DELETE FROM
        runtime_session`` would take these rows anyway. This runs first and
        returns the count, which turns the cascade from something the schema
        happens to do into something the purge states and audits. It also
        keeps the guarantee true on a database whose constraint was created
        before the cascade existed.
        """
        wanted = [value for value in runtime_session_ids if value is not None]
        if not wanted:
            return 0
        result = db.execute(
            delete(SessionSearchDocument)
            .where(SessionSearchDocument.runtime_session_id.in_(wanted))
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    def withhold_source_text(
        self, db: Session, *, source_kind: str, source_id: str
    ) -> int:
        """Clear the stored text of one source's chunks and mark them withheld.

        The rows stay so that a search still knows the content existed, when
        it happened and in which session, which is what makes a redaction
        legible rather than indistinguishable from a gap. The text itself is
        overwritten, because a redaction that only hides a column from one
        query is not a redaction.
        """
        result = db.execute(
            update(SessionSearchDocument)
            .where(
                SessionSearchDocument.source_kind == source_kind,
                SessionSearchDocument.source_id == str(source_id),
            )
            .values(
                content="",
                content_hash=content_hash_for(""),
                redaction_state=REDACTION_STATE_WITHHELD,
            )
            .execution_options(synchronize_session=False)
        )
        db.flush()
        return int(result.rowcount or 0)

    def count_orphans_for_sessions(self, db: Session) -> int:
        """Chunks whose runtime session is gone. Always zero, by construction."""
        return int(
            db.execute(
                select(func.count(SessionSearchDocument.id)).where(
                    ~select(RuntimeSession.id)
                    .where(
                        RuntimeSession.id == SessionSearchDocument.runtime_session_id
                    )
                    .exists()
                )
            ).scalar_one()
        )

    def count_orphans_for_sources(
        self,
        db: Session,
        *,
        source_kind: str,
        source_model: Any,
        excluding_held_sessions: bool = False,
    ) -> int:
        """Chunks of one kind whose source row is gone.

        ``source_id`` is text because the corpus indexes sources with
        different key types, so the join casts the source id to text rather
        than the chunk's id to a uuid: a malformed id then fails to match
        instead of failing the statement.

        ``excluding_held_sessions`` matches :meth:`delete_for_sources`: a
        chunk whose session is under legal hold is not an orphan of a
        purged usage row. The operator check after a usage pass would
        otherwise fail on the state the hold is designed to produce.
        """
        clauses: List[Any] = [
            SessionSearchDocument.source_kind == source_kind,
            ~select(source_model.id)
            .where(
                func.cast(source_model.id, String) == SessionSearchDocument.source_id
            )
            .exists(),
        ]
        if excluding_held_sessions:
            clauses.append(~_held_session_exists())
        return int(
            db.execute(
                select(func.count(SessionSearchDocument.id)).where(*clauses)
            ).scalar_one()
        )

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

    @staticmethod
    def _search_filters(
        *,
        account_id: Any,
        query: Optional[str] = None,
        runtime_session_id: Optional[Any] = None,
        source_kind: Optional[str] = None,
        role: Optional[str] = None,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
    ) -> List[Any]:
        """Predicates shared by the row query and the guarded hit query.

        One builder, so the guarded read cannot drift away from the raw one
        and start matching a wider set of rows than the query it is meant to
        be the safe version of.
        """
        clauses: List[Any] = [SessionSearchDocument.account_id == account_id]
        if runtime_session_id is not None:
            clauses.append(
                SessionSearchDocument.runtime_session_id == runtime_session_id
            )
        if source_kind:
            clauses.append(SessionSearchDocument.source_kind == source_kind)
        if role:
            clauses.append(SessionSearchDocument.role == role)
        if start_date:
            clauses.append(SessionSearchDocument.occurred_at >= start_date)
        if end_date:
            clauses.append(SessionSearchDocument.occurred_at < end_date)

        normalized_query = " ".join(query.strip().split()) if query else None
        if normalized_query:
            clauses.append(
                SessionSearchDocument.search_vector.op("@@")(
                    func.websearch_to_tsquery("simple", normalized_query)
                )
            )
        return clauses

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
        """Return account scoped chunk rows, newest first, optionally matched.

        The account filter is applied to the corpus table itself rather than
        to a joined source row, so a session id belonging to another account
        matches nothing whatever else is passed.

        This returns whole rows, withheld text included, and is therefore an
        internal accessor: a search response is built from
        :meth:`search_account_hits`.
        """
        return (
            db.query(SessionSearchDocument)
            .filter(
                *self._search_filters(
                    account_id=account_id,
                    query=query,
                    runtime_session_id=runtime_session_id,
                    source_kind=source_kind,
                    role=role,
                    start_date=start_date,
                    end_date=end_date,
                )
            )
            .order_by(
                SessionSearchDocument.occurred_at.desc(),
                SessionSearchDocument.chunk_index.asc(),
            )
            .limit(limit)
            .offset(offset)
            .all()
        )

    def search_account_hits(
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
    ) -> List[SessionSearchHit]:
        """The read seam a search response is built from.

        Same filters as :meth:`search_account_chunks`, but the projection
        replaces ``content`` with the empty string for any chunk whose
        redaction state is not in
        :data:`~preloop.models.models.session_search_document.TEXT_RETURNABLE_REDACTION_STATES`.
        The withheld text is therefore not merely unread: it never leaves the
        database, so nothing downstream, including a log line or an exception
        rendering the row, can spill it.

        Every surface that answers a user with corpus content is expected to
        call this. :meth:`search_account_chunks` returns rows and stays for
        internal callers that need the whole record.
        """
        returnable = SessionSearchDocument.redaction_state.in_(
            TEXT_RETURNABLE_REDACTION_STATES
        )
        guarded_content = case(
            (returnable, SessionSearchDocument.content),
            else_="",
        ).label("content")
        stmt = (
            select(
                SessionSearchDocument.id,
                SessionSearchDocument.runtime_session_id,
                SessionSearchDocument.source_kind,
                SessionSearchDocument.source_id,
                SessionSearchDocument.chunk_index,
                SessionSearchDocument.occurred_at,
                SessionSearchDocument.role,
                guarded_content,
                SessionSearchDocument.redaction_state,
                SessionSearchDocument.model_alias,
                SessionSearchDocument.provider_name,
                SessionSearchDocument.flow_id,
                SessionSearchDocument.status,
                SessionSearchDocument.meta_data,
            )
            .where(
                *self._search_filters(
                    account_id=account_id,
                    query=query,
                    runtime_session_id=runtime_session_id,
                    source_kind=source_kind,
                    role=role,
                    start_date=start_date,
                    end_date=end_date,
                )
            )
            .order_by(
                SessionSearchDocument.occurred_at.desc(),
                SessionSearchDocument.chunk_index.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
        return [
            SessionSearchHit(
                id=row.id,
                runtime_session_id=row.runtime_session_id,
                source_kind=row.source_kind,
                source_id=row.source_id,
                chunk_index=row.chunk_index,
                occurred_at=row.occurred_at,
                role=row.role,
                content=row.content or "",
                redaction_state=row.redaction_state,
                text_withheld=row.redaction_state
                not in TEXT_RETURNABLE_REDACTION_STATES,
                model_alias=row.model_alias,
                provider_name=row.provider_name,
                flow_id=row.flow_id,
                status=row.status,
                meta_data=row.meta_data,
            )
            for row in db.execute(stmt).all()
        ]

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

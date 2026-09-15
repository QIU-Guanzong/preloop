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
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import String, and_, case, delete, func, or_, select, update
from sqlalchemy.orm import Session

from preloop.config import settings

from ..models.api_usage import ApiUsage
from ..models.flow import Flow
from ..models.flow_execution import FlowExecution
from ..models.runtime_session import RuntimeSession
from ..models.session_search_document import (
    EMBEDDING_STATE_EMBEDDED,
    EMBEDDING_STATE_FAILED,
    EMBEDDING_STATE_IN_PROGRESS,
    EMBEDDING_STATE_PENDING,
    REDACTION_STATE_CLEAR,
    REDACTION_STATE_WITHHELD,
    TEXT_RETURNABLE_REDACTION_STATES,
    SessionSearchDocument,
)
from .base import CRUDBase


def _held_session_exists() -> Any:
    """True when the chunk's session is under legal hold.

    Shared by the usage-purge delete, the released-orphan sweep, and the
    usage-orphan count so those paths cannot disagree about which chunks a
    hold is allowed to keep after the usage row they quote is gone.
    """
    return (
        select(RuntimeSession.id)
        .where(RuntimeSession.id == SessionSearchDocument.runtime_session_id)
        .where(RuntimeSession.legal_hold.is_(True))
        .exists()
    )


def _source_row_gone(source_model: Any) -> Any:
    """True when no row of ``source_model`` matches the chunk's source id."""
    return ~(
        select(source_model.id)
        .where(func.cast(source_model.id, String) == SessionSearchDocument.source_id)
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

    def delete_orphans_for_sources(
        self,
        db: Session,
        *,
        source_kind: str,
        source_model: Any,
        excluding_held_sessions: bool = False,
        account_id: Optional[Any] = None,
    ) -> int:
        """Delete chunks of one kind whose source row is gone.

        The usage pass calls this after its batch delete. A held session's
        gateway chunks survive the pass that removed the usage row they
        quote; once the hold is released those usage ids never appear in a
        later batch, so only this sweep can reclaim them. The hold
        exclusion is the same EXISTS predicate as
        :meth:`delete_for_sources` and :meth:`count_orphans_for_sources`.
        """
        clauses: List[Any] = [
            SessionSearchDocument.source_kind == source_kind,
            _source_row_gone(source_model),
        ]
        if excluding_held_sessions:
            clauses.append(~_held_session_exists())
        if account_id is not None:
            clauses.append(SessionSearchDocument.account_id == account_id)
        result = db.execute(
            delete(SessionSearchDocument)
            .where(*clauses)
            .execution_options(synchronize_session=False)
        )
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
            _source_row_gone(source_model),
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

        A session is held when ``runtime_session.legal_hold`` is true, or
        when any of its metered gateway rows belongs to a flow execution
        under hold. The column is the stronger check (issue #650); the
        execution path covers sessions whose hold was recorded only on the
        flow.

        Evaluated as a set rather than a join in the claim query because the
        common case is an empty set, and an empty set costs one cheap index
        lookup instead of a correlated subquery per candidate chunk.
        """
        held: set[str] = {
            str(value)
            for value in db.execute(
                select(RuntimeSession.id).where(
                    RuntimeSession.account_id == account_id,
                    RuntimeSession.legal_hold.is_(True),
                )
            ).scalars()
            if value is not None
        }
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
        held.update(str(value) for value in rows if value is not None)
        return held

    def _stale_claim_cutoff(self, now: Optional[datetime] = None) -> datetime:
        """When an ``in_progress`` claim is old enough to reclaim.

        The window is twice the provider timeout plus a 30s margin, so a
        live call cannot be stolen by another worker, but a daemon-thread
        death or unexpected exception cannot strand the batch forever.
        Compared as naive UTC to match ``Base.updated_at``.
        """
        timeout = float(getattr(settings, "session_embedding_timeout_seconds", 30.0))
        window = timedelta(seconds=max(1.0, (2.0 * timeout) + 30.0))
        stamp = now or datetime.now(UTC)
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(UTC).replace(tzinfo=None)
        return stamp - window

    def _embeddable_filters(
        self, *, account_id: Any, now: Optional[datetime] = None
    ) -> list[Any]:
        """Conditions every embeddable chunk must satisfy.

        Only ``clear`` chunks qualify. A redacted chunk has had a credential
        masked and a metadata-only chunk never captured content at all;
        embedding either would put a vector of the mask, or of a descriptor,
        into a corpus that a semantic query then treats as the real thing.

        ``pending`` rows are always claimable. ``in_progress`` rows whose
        ``updated_at`` is older than the reclaim window are claimable too,
        so a worker crash between the claim commit and store cannot hide
        the backlog from the pending count or from the next run.
        """
        stale_before = self._stale_claim_cutoff(now)
        claimable_state = or_(
            SessionSearchDocument.embedding_state == EMBEDDING_STATE_PENDING,
            and_(
                SessionSearchDocument.embedding_state == EMBEDDING_STATE_IN_PROGRESS,
                SessionSearchDocument.updated_at < stale_before,
            ),
        )
        return [
            SessionSearchDocument.account_id == account_id,
            claimable_state,
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
        now: Optional[datetime] = None,
    ) -> int:
        """How many chunks this account still has waiting for a vector."""
        stmt = db.query(func.count(SessionSearchDocument.id)).filter(
            *self._embeddable_filters(account_id=account_id, now=now)
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
        now: Optional[datetime] = None,
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
        A claim left ``in_progress`` past the reclaim window is treated as
        pending again, so a restart cannot strand the batch.
        """
        if limit <= 0:
            return []
        excluded = [str(value) for value in (excluded_session_ids or [])]
        filters = self._embeddable_filters(account_id=account_id, now=now)

        oldest_stmt = db.query(SessionSearchDocument.runtime_session_id).filter(
            *filters
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
                *filters,
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

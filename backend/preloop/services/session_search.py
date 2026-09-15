"""Ranked keyword search over the runtime session corpus.

This is the query half of the corpus written by the indexing service. It
answers "where did an agent do this", which is a relevance question, not a
recency one: the session that matched in five places is almost always the one
the operator wanted, and a timestamp ordering buries it under whatever ran
last night.

Everything account scoped happens in the CRUD query, not here. This module
turns a validated request into CRUD arguments, times the call, and states in
the response what the answer could not do.
"""

from __future__ import annotations

import logging
import time
from typing import Any, List, Optional

from sqlalchemy.orm import Session

from preloop.models.crud import crud_session_search_document
from preloop.models.crud.session_search_document import (
    RankedSession,
    RankedSnippet,
)
from preloop.models.crud.session_search_document import (
    SessionSearchFilters as CrudSessionSearchFilters,
)
from preloop.schemas.session_search import (
    DEGRADED_SEMANTIC_NOT_ENABLED,
    SessionSearchDegraded,
    SessionSearchMode,
    SessionSearchRequest,
    SessionSearchResponse,
    SessionSearchResult,
    SessionSearchSnippet,
)

logger = logging.getLogger(__name__)

#: The only mode that can actually run today. Kept as a constant so the one
#: place that decides "semantic is not available" is greppable when #657
#: flips it.
EFFECTIVE_MODE: SessionSearchMode = "keyword"

_SEMANTIC_DETAIL = (
    "Semantic ranking is not enabled on this deployment; these are keyword "
    "results ranked by relevance."
)


def _to_crud_filters(request: SessionSearchRequest) -> CrudSessionSearchFilters:
    """Translate the validated filter block into CRUD filter arguments."""
    filters = request.filters
    return CrudSessionSearchFilters(
        start_date=filters.start_date,
        end_date=filters.end_date,
        model_alias=filters.model_alias,
        provider_name=filters.provider_name,
        runtime_principal_id=filters.runtime_principal_id,
        api_key_id=filters.api_key_id,
        flow_id=filters.flow_id,
        source_kind=filters.source_kind,
    )


def _degraded_for(request: SessionSearchRequest) -> SessionSearchDegraded:
    """Describe what this answer is, and is not, for the requested mode.

    A mode the deployment cannot serve is answered, not refused: a caller
    written against the eventual hybrid contract keeps working today and gets
    better answers later without changing a line, and the marker tells them
    which of the two they are reading.
    """
    if request.mode == EFFECTIVE_MODE:
        return SessionSearchDegraded(
            keyword=True, semantic=False, reasons=[], detail=None
        )
    return SessionSearchDegraded(
        keyword=True,
        semantic=False,
        reasons=[DEGRADED_SEMANTIC_NOT_ENABLED],
        detail=_SEMANTIC_DETAIL,
    )


def _snippet_to_schema(snippet: RankedSnippet) -> SessionSearchSnippet:
    """Serialise one matching chunk."""
    return SessionSearchSnippet(
        document_id=snippet.document_id,
        runtime_session_id=snippet.runtime_session_id,
        source_kind=snippet.source_kind,
        source_id=snippet.source_id,
        chunk_index=snippet.chunk_index,
        occurred_at=snippet.occurred_at,
        role=snippet.role,
        rank=snippet.rank,
        redaction_state=snippet.redaction_state,
        text=snippet.text,
    )


def _session_to_schema(session: RankedSession) -> SessionSearchResult:
    """Serialise one ranked session and its snippets."""
    return SessionSearchResult(
        runtime_session_id=session.runtime_session_id,
        session_source_type=session.session_source_type,
        session_source_id=session.session_source_id,
        session_reference=session.session_reference,
        title=session.title,
        started_at=session.started_at,
        last_activity_at=session.last_activity_at,
        score=session.score,
        best_chunk_rank=session.best_chunk_rank,
        matched_chunk_count=session.matched_chunk_count,
        first_match_at=session.first_match_at,
        last_match_at=session.last_match_at,
        snippets=[_snippet_to_schema(snippet) for snippet in session.snippets],
    )


def search_sessions(
    db: Session,
    *,
    account_id: Any,
    request: SessionSearchRequest,
) -> SessionSearchResponse:
    """Run one ranked search and build the response.

    Args:
        db: Request scoped session.
        account_id: The caller's account. Passed to the CRUD query, which
            binds it in SQL; nothing downstream filters by account.
        request: The validated request body.

    Returns:
        The ranked page, the total session count, the corpus freshness marker
        and a degraded block describing what did not run.
    """
    started = time.perf_counter()
    indexed_through: Optional[Any] = crud_session_search_document.indexed_through(
        db, account_id=account_id
    )
    sessions, total = crud_session_search_document.search_sessions_ranked(
        db,
        account_id=account_id,
        query=request.query,
        filters=_to_crud_filters(request),
        limit=request.limit,
        offset=request.offset,
        max_snippets_per_session=request.max_snippets_per_session,
        include_snippet_text=request.include_snippet_text,
    )
    results: List[SessionSearchResult] = [
        _session_to_schema(session) for session in sessions
    ]
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    # The query text is never logged. It is the operator's search over their
    # own transcripts, which is exactly the string that should not end up in a
    # log line any more than in an access log.
    logger.debug(
        "Session search returned %d of %d sessions in %.1f ms",
        len(results),
        total,
        elapsed_ms,
    )
    return SessionSearchResponse(
        query=request.query,
        mode=request.mode,
        effective_mode=EFFECTIVE_MODE,
        degraded=_degraded_for(request),
        indexed_through=indexed_through,
        total=total,
        limit=request.limit,
        offset=request.offset,
        elapsed_ms=round(elapsed_ms, 3),
        results=results,
    )

"""Write runtime session content into the chunked search corpus.

Every writer here is called from a path that already persists the source row,
runs inside a savepoint and never raises: an indexing failure must not fail
the call that produced the content, which is how the shipped gateway auto
indexing call site already behaves.

Chunking is deliberately boring and deterministic. Text is normalised, then
cut into ``CHUNK_SIZE_CHARS`` windows that advance by
``CHUNK_SIZE_CHARS - CHUNK_OVERLAP_CHARS`` characters, with the cut pulled
back to the last line or word boundary in the tail of the window when there is
one. The same input always produces the same chunks, so re-indexing a source
whose content did not change hashes identically and writes nothing.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import crud_session_search_document
from preloop.models.crud.session_search_document import SessionSearchChunk
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.session_search_document import (
    EMBEDDING_STATE_PENDING,
    REDACTION_STATE_CLEAR,
    REDACTION_STATE_METADATA_ONLY,
    REDACTION_STATE_REDACTED,
    SOURCE_KIND_FLOW_LOG,
    SOURCE_KIND_GATEWAY_INTERACTION,
    SOURCE_KIND_OPERATOR_NOTE,
    SOURCE_KIND_SESSION_SUMMARY,
    SOURCE_KIND_TOOL_CALL,
    SOURCE_KIND_TRANSCRIPT_MESSAGE,
    SessionSearchDocument,
)
from preloop.services.gateway_usage_search import GatewayUsageSearchService

logger = logging.getLogger(__name__)

#: Chunk size and overlap, in characters, applied to every source kind.
#: 1200 characters is roughly a screen of transcript and comfortably below the
#: 1 MB ``tsvector`` limit even for dense text; the 200 character overlap keeps
#: a phrase that straddles a cut findable in at least one chunk.
CHUNK_SIZE_CHARS = 1200
CHUNK_OVERLAP_CHARS = 200
#: A single source never produces more than this many chunks. A runaway
#: payload costs a bounded number of rows, and the cut is deterministic.
MAX_CHUNKS_PER_SOURCE = 64
#: Mask applied to a credential-looking value found in free text.
REDACTED_VALUE = GatewayUsageSearchService.REDACTED_VALUE

_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b([\w.-]*(?:api[_-]?key|authorization|secret|token|password)"
    r"[\w.-]*)\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def indexing_enabled() -> bool:
    """Return whether the corpus accepts writes at all.

    The kill switch is checked on every write rather than cached, so flipping
    ``SESSION_SEARCH_INDEX_ENABLED`` stops indexing without a restart of a
    long lived worker.
    """
    return bool(getattr(settings, "session_search_index_enabled", True))


def chunk_text(text: str) -> List[str]:
    """Split normalised text into deterministic overlapping chunks."""
    normalized = (text or "").strip()
    if not normalized:
        return []
    if len(normalized) <= CHUNK_SIZE_CHARS:
        return [normalized]

    chunks: List[str] = []
    start = 0
    length = len(normalized)
    while start < length and len(chunks) < MAX_CHUNKS_PER_SOURCE:
        end = min(start + CHUNK_SIZE_CHARS, length)
        window = normalized[start:end]
        if end < length:
            # Pull the cut back to the last line or word boundary in the tail
            # of the window, so a chunk ends where a reader would end it.
            tail_start = max(len(window) - CHUNK_OVERLAP_CHARS, 0)
            boundary = max(
                window.rfind("\n", tail_start),
                window.rfind(" ", tail_start),
            )
            if boundary > 0:
                end = start + boundary
                window = normalized[start:end]
        chunk = window.strip()
        if chunk:
            chunks.append(chunk)
        if end >= length:
            break
        start = max(end - CHUNK_OVERLAP_CHARS, start + 1)
    return chunks


def redact_text(text: str) -> tuple[str, bool]:
    """Mask credential-looking values in free text.

    Returns the text and whether anything was masked. Gateway payloads are
    sanitised by :class:`GatewayUsageSearchService` before they get here and
    are not touched again; this covers the free text sources (transcript
    messages, tool call summaries, operator notes) that never pass through a
    payload sanitiser.
    """
    if not text:
        return "", False
    redacted, count = _CREDENTIAL_PATTERN.subn(
        lambda match: f"{match.group(1)}: {REDACTED_VALUE}", text
    )
    return redacted, bool(count)


def _resolve_redaction_state(*, captured: bool, redacted: bool) -> str:
    if not captured:
        return REDACTION_STATE_METADATA_ONLY
    return REDACTION_STATE_REDACTED if redacted else REDACTION_STATE_CLEAR


def _build_chunks(
    *,
    text: str,
    redaction_state: str,
    role: Optional[str],
    meta_data: Optional[Dict[str, Any]],
    model_alias: Optional[str] = None,
    provider_name: Optional[str] = None,
    runtime_principal_id: Optional[str] = None,
    api_key_id: Optional[Any] = None,
    flow_id: Optional[Any] = None,
    status: Optional[str] = None,
) -> List[SessionSearchChunk]:
    pieces = chunk_text(text)
    total = len(pieces)
    return [
        SessionSearchChunk(
            content=piece,
            chunk_index=index,
            role=role,
            redaction_state=redaction_state,
            embedding_state=EMBEDDING_STATE_PENDING,
            model_alias=model_alias,
            provider_name=provider_name,
            runtime_principal_id=runtime_principal_id,
            api_key_id=api_key_id,
            flow_id=flow_id,
            status=status,
            meta_data={**(meta_data or {}), "chunk_count": total},
        )
        for index, piece in enumerate(pieces)
    ]


def request_embedding(account_id: Any) -> None:
    """Nudge the embedding worker after the host transaction has committed.

    ``write_source_chunks`` calls this only on ``commit=True``. Callers that
    write chunks inside someone else's transaction (transcript import) must
    invoke this themselves after they commit, so imported chunks do not wait
    for an unrelated later write. The submission is still deduplicated and
    dropped when the queue is full; embedding runs on the worker's own
    session.
    """
    try:
        from preloop.services.session_embedding_queue import (
            submit_account_for_embedding,
        )

        submit_account_for_embedding(account_id)
    except Exception:  # noqa: BLE001 - indexing never fails over embedding
        logger.warning(
            "Session embedding hand-off failed for account %s",
            account_id,
            exc_info=True,
        )


def _request_embedding(account_id: Any, stored: List[SessionSearchDocument]) -> None:
    """Nudge after this writer committed pending chunks.

    Callers must invoke this only after the host transaction has committed.
    A nudge while the writer's transaction is still open wakes the worker
    on rows it cannot see, burns a submission, and leaves the chunks
    waiting for a later write.
    """
    if not stored:
        return
    if not any(row.embedding_state == EMBEDDING_STATE_PENDING for row in stored):
        return
    request_embedding(account_id)


def write_source_chunks(
    db: Session,
    *,
    account_id: Any,
    runtime_session_id: Any,
    source_kind: str,
    source_id: Any,
    text: str,
    occurred_at: Optional[datetime] = None,
    role: Optional[str] = None,
    content_captured: bool = True,
    already_sanitised: bool = False,
    meta_data: Optional[Dict[str, Any]] = None,
    model_alias: Optional[str] = None,
    provider_name: Optional[str] = None,
    runtime_principal_id: Optional[str] = None,
    api_key_id: Optional[Any] = None,
    flow_id: Optional[Any] = None,
    status: Optional[str] = None,
    commit: bool = False,
) -> List[SessionSearchDocument]:
    """Write one source's chunks, swallowing every failure.

    The write runs inside a savepoint: a failure rolls back the chunk rows
    only and leaves the caller's transaction usable, which is the difference
    between a missing search hit and a lost gateway call.
    """
    if not indexing_enabled():
        return []
    if account_id is None or runtime_session_id is None or source_id is None:
        return []

    try:
        redacted = False
        if content_captured and not already_sanitised:
            text, redacted = redact_text(text)
        elif content_captured:
            redacted = REDACTED_VALUE in (text or "")
        redaction_state = _resolve_redaction_state(
            captured=content_captured, redacted=redacted
        )
        chunks = _build_chunks(
            text=text,
            redaction_state=redaction_state,
            role=role,
            meta_data={
                **(meta_data or {}),
                "content_captured": bool(content_captured),
            },
            model_alias=model_alias,
            provider_name=provider_name,
            runtime_principal_id=runtime_principal_id,
            api_key_id=api_key_id,
            flow_id=flow_id,
            status=status,
        )
        if not chunks:
            return []

        savepoint = db.begin_nested()
        try:
            stored = crud_session_search_document.replace_source_chunks(
                db,
                account_id=account_id,
                runtime_session_id=runtime_session_id,
                source_kind=source_kind,
                source_id=str(source_id),
                occurred_at=occurred_at or _now(),
                chunks=chunks,
            )
        except Exception:
            if savepoint.is_active:
                savepoint.rollback()
            raise
        else:
            if savepoint.is_active:
                savepoint.commit()
        if commit:
            db.commit()
            _request_embedding(account_id, stored)
        return stored
    except Exception:  # noqa: BLE001 - indexing never fails its caller
        logger.warning(
            "Session search indexing failed for %s %s",
            source_kind,
            source_id,
            exc_info=True,
        )
        return []


def index_gateway_interaction(
    db: Session,
    *,
    usage: ApiUsage,
    request_payload: Optional[Dict[str, Any]] = None,
    response_payload: Optional[Dict[str, Any]] = None,
    commit: bool = False,
) -> List[SessionSearchDocument]:
    """Index one metered gateway interaction as session chunks.

    The text is built by the shipped gateway search service, sanitiser
    included and unchanged, so the corpus and the gateway document agree on
    what a captured interaction looks like.
    """
    if not indexing_enabled():
        return []
    try:
        runtime_session_id = getattr(usage, "runtime_session_id", None)
        if runtime_session_id is None:
            return []
        service = GatewayUsageSearchService(db)
        content_captured = bool(settings.model_gateway_capture_content)
        prepared_request = (
            service.payload_for_indexing(request_payload) if content_captured else None
        )
        prepared_response = (
            service.payload_for_indexing(response_payload) if content_captured else None
        )
        text = service.build_searchable_text(
            usage=usage,
            request_payload=prepared_request,
            response_payload=prepared_response,
        )
        meta_data = service.build_document_metadata(
            usage=usage,
            request_payload=prepared_request,
            response_payload=prepared_response,
        )
    except Exception:  # noqa: BLE001 - indexing never fails its caller
        logger.warning(
            "Session search text build failed for gateway usage %s",
            getattr(usage, "id", None),
            exc_info=True,
        )
        return []

    return write_source_chunks(
        db,
        account_id=usage.account_id,
        runtime_session_id=runtime_session_id,
        source_kind=SOURCE_KIND_GATEWAY_INTERACTION,
        source_id=usage.id,
        text=text,
        occurred_at=usage.timestamp,
        role="assistant",
        content_captured=content_captured,
        already_sanitised=True,
        meta_data=meta_data,
        model_alias=usage.model_alias,
        provider_name=usage.provider_name,
        runtime_principal_id=usage.runtime_principal_id,
        api_key_id=usage.api_key_id,
        flow_id=usage.flow_id,
        status=str(usage.status_code) if usage.status_code is not None else None,
        commit=commit,
    )


def index_transcript_message(
    db: Session,
    *,
    account_id: Any,
    runtime_session_id: Any,
    source_id: Any,
    text: str,
    role: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
    meta_data: Optional[Dict[str, Any]] = None,
    commit: bool = False,
) -> List[SessionSearchDocument]:
    """Index one pushed transcript message."""
    content_captured = bool(settings.model_gateway_capture_content)
    return write_source_chunks(
        db,
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        source_kind=SOURCE_KIND_TRANSCRIPT_MESSAGE,
        source_id=source_id,
        text=text
        if content_captured
        else _descriptor(kind=SOURCE_KIND_TRANSCRIPT_MESSAGE, role=role, text=text),
        occurred_at=occurred_at,
        role=role,
        content_captured=content_captured,
        meta_data=meta_data,
        status=role,
        commit=commit,
    )


def index_tool_call(
    db: Session,
    *,
    account_id: Any,
    runtime_session_id: Any,
    source_id: Any,
    server_name: Optional[str],
    tool_name: Optional[str],
    status: Optional[str],
    summary: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
    api_key_id: Optional[Any] = None,
    flow_id: Optional[Any] = None,
    meta_data: Optional[Dict[str, Any]] = None,
    commit: bool = False,
) -> List[SessionSearchDocument]:
    """Index one tool call activity."""
    content_captured = bool(settings.model_gateway_capture_content)
    header = "\n".join(
        line
        for line in (
            f"kind: {SOURCE_KIND_TOOL_CALL}",
            f"server_name: {server_name}" if server_name else "",
            f"tool_name: {tool_name}" if tool_name else "",
            f"status: {status}" if status else "",
        )
        if line
    )
    text = f"{header}\n{summary}" if (summary and content_captured) else header
    return write_source_chunks(
        db,
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        source_kind=SOURCE_KIND_TOOL_CALL,
        source_id=source_id,
        text=text,
        occurred_at=occurred_at,
        role="tool",
        content_captured=content_captured,
        meta_data=meta_data,
        api_key_id=api_key_id,
        flow_id=flow_id,
        status=status,
        commit=commit,
    )


def index_operator_note(
    db: Session,
    *,
    account_id: Any,
    runtime_session_id: Any,
    source_id: Any,
    body: Optional[str],
    status: Optional[str] = None,
    occurred_at: Optional[datetime] = None,
    meta_data: Optional[Dict[str, Any]] = None,
    commit: bool = False,
) -> List[SessionSearchDocument]:
    """Index one operator note as it reaches a session."""
    content_captured = bool(settings.model_gateway_capture_content)
    text = (
        f"kind: {SOURCE_KIND_OPERATOR_NOTE}\n{body}"
        if (body and content_captured)
        else _descriptor(
            kind=SOURCE_KIND_OPERATOR_NOTE, role="operator", text=body or ""
        )
    )
    return write_source_chunks(
        db,
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        source_kind=SOURCE_KIND_OPERATOR_NOTE,
        source_id=source_id,
        text=text,
        occurred_at=occurred_at,
        role="operator",
        content_captured=content_captured,
        meta_data=meta_data,
        status=status,
        commit=commit,
    )


def index_session_summary(
    db: Session,
    *,
    account_id: Any,
    runtime_session_id: Any,
    title: Optional[str],
    summary: Optional[str],
    occurred_at: Optional[datetime] = None,
    meta_data: Optional[Dict[str, Any]] = None,
    commit: bool = False,
) -> List[SessionSearchDocument]:
    """Index a session's own title and summary."""
    content_captured = bool(settings.model_gateway_capture_content)
    body = "\n".join(
        line
        for line in (
            f"kind: {SOURCE_KIND_SESSION_SUMMARY}",
            f"title: {title}" if title else "",
            f"summary: {summary}" if (summary and content_captured) else "",
        )
        if line
    )
    return write_source_chunks(
        db,
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        source_kind=SOURCE_KIND_SESSION_SUMMARY,
        source_id=runtime_session_id,
        text=body,
        occurred_at=occurred_at,
        role="system",
        content_captured=content_captured,
        meta_data=meta_data,
        commit=commit,
    )


def index_flow_log(
    db: Session,
    *,
    account_id: Any,
    runtime_session_id: Any,
    source_id: Any,
    text: str,
    occurred_at: Optional[datetime] = None,
    flow_id: Optional[Any] = None,
    meta_data: Optional[Dict[str, Any]] = None,
    commit: bool = False,
) -> List[SessionSearchDocument]:
    """Index one flow log excerpt attached to a session.

    No call site yet. Flow logs are the one source whose volume needs a
    sampling rule of its own before anything indexes it wholesale, so the
    kind and its writer land here and the decision lands with the caller.
    """
    content_captured = bool(settings.model_gateway_capture_content)
    return write_source_chunks(
        db,
        account_id=account_id,
        runtime_session_id=runtime_session_id,
        source_kind=SOURCE_KIND_FLOW_LOG,
        source_id=source_id,
        text=text
        if content_captured
        else _descriptor(kind=SOURCE_KIND_FLOW_LOG, role="system", text=text),
        occurred_at=occurred_at,
        role="system",
        content_captured=content_captured,
        flow_id=flow_id,
        meta_data=meta_data,
        commit=commit,
    )


def _descriptor(*, kind: str, role: Optional[str], text: str) -> str:
    """Return the metadata only stand in for content we may not store.

    A chunk written with capture disabled says what existed and how big it
    was. That keeps the row honest (and countable) instead of writing an
    empty chunk that looks like content nobody wrote.
    """
    lines = [f"kind: {kind}", "content_captured: false"]
    if role:
        lines.append(f"role: {role}")
    lines.append(f"content_length: {len(text or '')}")
    return "\n".join(lines)

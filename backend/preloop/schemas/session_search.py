"""Request and response shapes for ranked search over session content.

The request is a POST body rather than a query string on purpose. The text an
operator types here is whatever they are looking for in their own agent
transcripts, and a proxy access log is the last place that belongs. The two
existing GET search surfaces predate that reasoning; this one does not repeat
it.

``extra="forbid"`` on both the body and the filter block is the other
deliberate choice: an unrecognised filter key is a validation error, because a
silently dropped filter returns a wider result set than the caller believes
they asked for, and they have no way to tell.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from preloop.models.crud.session_search_document import (
    MAX_SESSION_RESULTS,
    MAX_SNIPPETS_PER_SESSION,
    normalize_query,
)
from preloop.models.models.session_search_document import SOURCE_KINDS

#: Search modes the contract accepts. Only ``keyword`` is implemented; the
#: other two are accepted today and answered with keyword results plus a
#: degraded marker, so the contract does not change shape when semantic
#: ranking lands.
SessionSearchMode = Literal["keyword", "semantic", "hybrid"]

#: Reason code published in the degraded block for a mode that needs
#: embeddings nothing computes yet.
DEGRADED_SEMANTIC_NOT_ENABLED = "semantic_not_enabled"

#: Longest query accepted. Past this a caller is pasting a document, not
#: searching for one.
MAX_QUERY_CHARS = 512

#: Default page size, and the default number of snippets per session.
DEFAULT_SESSION_RESULTS = 20
DEFAULT_SNIPPETS_PER_SESSION = 3


class SessionSearchFilters(BaseModel):
    """Filters over the denormalised columns the corpus carries.

    Everything here is a column on the corpus row itself, snapshotted by the
    writer, so filtering never joins a source table and never reaches outside
    the account bound applied by the query.
    """

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    start_date: Optional[datetime] = Field(
        None,
        description=(
            "Only chunks at or after this instant. Must include a timezone "
            "offset; a naive value is rejected."
        ),
    )
    end_date: Optional[datetime] = Field(
        None,
        description=(
            "Only chunks strictly before this instant. Must include a "
            "timezone offset; a naive value is rejected."
        ),
    )
    model_alias: Optional[str] = Field(
        None,
        min_length=1,
        description="Model alias recorded on the chunk, for example a gpt-5 alias.",
    )
    provider_name: Optional[str] = Field(
        None, min_length=1, description="Provider recorded on the chunk."
    )
    runtime_principal_id: Optional[str] = Field(
        None,
        min_length=1,
        description="Runtime principal, the agent or user the session ran as.",
    )
    api_key_id: Optional[UUID] = Field(
        None, description="API key the traffic was attributed to."
    )
    flow_id: Optional[UUID] = Field(None, description="Flow the session belonged to.")
    source_kind: Optional[str] = Field(
        None,
        description=("One corpus source kind: " + ", ".join(SOURCE_KINDS) + "."),
    )

    @field_validator("source_kind")
    @classmethod
    def validate_source_kind(cls, value: Optional[str]) -> Optional[str]:
        """Reject an unknown source kind rather than matching nothing."""
        if value is None:
            return None
        if value not in SOURCE_KINDS:
            raise ValueError("source_kind must be one of: " + ", ".join(SOURCE_KINDS))
        return value

    @field_validator("start_date", "end_date")
    @classmethod
    def require_timezone_aware_bounds(
        cls, value: Optional[datetime]
    ) -> Optional[datetime]:
        """Reject a naive bound so the same body is the same instant everywhere."""
        if value is None:
            return None
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("start_date and end_date must include a timezone offset")
        return value


class SessionSearchRequest(BaseModel):
    """One ranked search over the caller's own session corpus."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=1,
        max_length=MAX_QUERY_CHARS,
        description=(
            "Search text, parsed the way a web search box is: a quoted "
            'phrase ("rolling restart") stays a phrase, `or` alternates and a '
            "leading `-` excludes."
        ),
    )
    mode: SessionSearchMode = Field(
        "keyword",
        description=(
            "Requested ranking mode. Anything other than keyword is answered "
            "with keyword results and a degraded marker until semantic "
            "ranking exists."
        ),
    )
    filters: SessionSearchFilters = Field(
        default_factory=SessionSearchFilters,
        description="Filters over the corpus columns. Unknown keys are rejected.",
    )
    limit: int = Field(
        DEFAULT_SESSION_RESULTS,
        ge=1,
        le=MAX_SESSION_RESULTS,
        description=f"Sessions per page, at most {MAX_SESSION_RESULTS}.",
    )
    offset: int = Field(0, ge=0, description="Sessions to skip.")
    max_snippets_per_session: int = Field(
        DEFAULT_SNIPPETS_PER_SESSION,
        ge=0,
        le=MAX_SNIPPETS_PER_SESSION,
        description=(
            "Snippets to return per session, at most "
            f"{MAX_SNIPPETS_PER_SESSION}. Zero returns scored sessions with "
            "no snippet rows at all."
        ),
    )
    include_snippet_text: bool = Field(
        True,
        description=(
            "When false the snippet text is never generated, so no captured "
            "content leaves the database; the snippets still carry the "
            "identity needed to open the session at that turn."
        ),
    )

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        """Collapse whitespace and reject a query that is only whitespace."""
        normalized = normalize_query(value)
        if not normalized:
            raise ValueError("query must contain at least one non-whitespace character")
        return normalized


class SessionSearchSnippet(BaseModel):
    """One matching chunk of one session.

    The identity fields are the point: ``runtime_session_id`` with
    ``source_kind`` and ``source_id`` names the turn the match came from, and
    ``chunk_index`` names the piece of it, so a console or a CLI can open the
    session at that turn rather than at the top.
    """

    document_id: UUID = Field(..., description="Corpus chunk row id.")
    runtime_session_id: UUID = Field(..., description="Session the chunk belongs to.")
    source_kind: str = Field(..., description="Which kind of turn this came from.")
    source_id: str = Field(..., description="Identifier of the turn in its own table.")
    chunk_index: int = Field(..., description="Position of the chunk inside the turn.")
    occurred_at: datetime = Field(..., description="When the turn happened.")
    role: Optional[str] = Field(None, description="Role recorded for the turn.")
    rank: float = Field(..., description="Relevance of this chunk on its own.")
    redaction_state: str = Field(
        ..., description="Whether the stored chunk was masked or metadata only."
    )
    text: Optional[str] = Field(
        None,
        description=(
            "Database generated headline with the matching terms marked, or "
            "null when the request disabled snippet text."
        ),
    )


class SessionSearchResult(BaseModel):
    """One session that matched, with its fused score."""

    runtime_session_id: UUID
    session_source_type: Optional[str] = None
    session_source_id: Optional[str] = None
    session_reference: Optional[str] = None
    title: Optional[str] = None
    started_at: Optional[datetime] = None
    last_activity_at: Optional[datetime] = None
    score: float = Field(..., description="Fused session score the ordering uses.")
    best_chunk_rank: float = Field(
        ..., description="Relevance of the single best chunk in this session."
    )
    matched_chunk_count: int = Field(
        ..., description="How many distinct chunks of this session matched."
    )
    first_match_at: Optional[datetime] = None
    last_match_at: Optional[datetime] = None
    snippets: List[SessionSearchSnippet] = Field(default_factory=list)


class SessionSearchDegraded(BaseModel):
    """What the answer could not do, stated rather than implied."""

    keyword: bool = Field(
        True, description="Whether keyword ranking contributed to this answer."
    )
    semantic: bool = Field(
        False, description="Whether semantic ranking contributed to this answer."
    )
    reasons: List[str] = Field(
        default_factory=list,
        description="Machine readable reason codes, empty when nothing degraded.",
    )
    detail: Optional[str] = Field(
        None, description="One sentence a console can show without decoding a code."
    )


class SessionSearchResponse(BaseModel):
    """Ranked sessions and everything needed to read them honestly."""

    query: str = Field(..., description="Query as parsed, with whitespace collapsed.")
    mode: SessionSearchMode = Field(..., description="Mode the caller asked for.")
    effective_mode: SessionSearchMode = Field(
        ..., description="Mode that actually ran."
    )
    degraded: SessionSearchDegraded
    indexed_through: Optional[datetime] = Field(
        None,
        description=(
            "Newest content this account has in the corpus. The corpus fills "
            "forward, so an empty answer older than this marker means no "
            "match, and one newer means not indexed yet."
        ),
    )
    total: int = Field(..., description="Distinct sessions matching, before paging.")
    limit: int
    offset: int
    elapsed_ms: float = Field(..., description="Server side time spent on the search.")
    results: List[SessionSearchResult] = Field(default_factory=list)

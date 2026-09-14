"""Explicit adoption contract for one previously published implementation."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

RecoveryMode = Literal["native_resume", "published_branch_handoff"]


class ContinuationPreview(BaseModel):
    execution_id: UUID
    flow_id: UUID
    pr_url: str
    branch: str
    head_sha: str
    feedback_enabled: bool
    artifact_upload_enabled: bool
    feedback_readable: bool = False
    feedback_blocked_reason: str | None = None
    native_resume_available: bool
    # When native resume is offered, the checkpoint window actually ends at
    # the earlier of the workspace snapshot and native session artifact
    # expiries. Surfacing it keeps the advertised recovery window aligned
    # with stored state (e.g. a 7-day feedback policy must not imply a
    # native conversation can still be resumed once the 24h checkpoint
    # artifacts are gone). None when native resume is unavailable.
    native_resume_expires_at: datetime | None = None
    existing_thread_id: UUID | None = None
    existing_thread_state: str | None = None
    allowed_recovery_modes: list[RecoveryMode]
    warnings: list[str]


class ContinuationAdoptRequest(BaseModel):
    recovery_mode: RecoveryMode
    expected_head_sha: str = Field(pattern=r"^[0-9a-fA-F]{40,64}$")
    acknowledge_fresh_conversation: bool = False
    # Operator-selected recovery when the runner lost the publication receipt.
    # Provider reads still use the execution's account/tracker/repository binding.
    pr_url: str | None = Field(default=None, min_length=1, max_length=2048)
    branch: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("pr_url")
    @classmethod
    def normalize_publication_url(cls, value: str | None) -> str | None:
        return value.rstrip("/") if value else value

    @model_validator(mode="after")
    def require_publication_pair(self) -> "ContinuationAdoptRequest":
        if (self.pr_url is None) != (self.branch is None):
            raise ValueError("Provide both the published PR URL and source branch")
        return self


class ContinuationAdoptResponse(BaseModel):
    thread_id: UUID
    state: str
    pr_url: str
    recovery_mode: RecoveryMode

"""Persistence for the per account session embedding opt in.

Every function is account scoped. Enabling is the only operation that takes
provider details, because naming the provider *is* the opt in: an account
cannot end up embedding against an endpoint nobody chose.
"""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime
from typing import Any, Optional
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from ..models.session_embedding_setting import (
    EMBEDDING_PROVIDERS,
    PROVIDER_LOCAL,
    PROVIDER_OPENAI_COMPATIBLE,
    SessionEmbeddingSetting,
)
from ..models.session_search_document import EMBEDDING_DIMENSIONS
from .base import CRUDBase


class SessionEmbeddingConfigError(ValueError):
    """The requested embedding configuration cannot be stored as asked."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def validate_openai_compatible_base_url(url: str) -> str:
    """Return a cleaned https URL, or raise if it is not safe to POST to.

    The worker may attach a deployment-wide API key to this URL, so the
    opt-in is the last moment to refuse a private, loopback, or link-local
    target and anything that is not https.
    """
    cleaned = (url or "").strip()
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() != "https":
        raise SessionEmbeddingConfigError(
            "invalid_base_url",
            "an OpenAI compatible base url must be https",
        )
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise SessionEmbeddingConfigError(
            "invalid_base_url",
            "an OpenAI compatible base url must include a host",
        )
    if parsed.username or parsed.password:
        raise SessionEmbeddingConfigError(
            "invalid_base_url",
            "an OpenAI compatible base url must not include credentials",
        )
    if host == "localhost" or host.endswith(".localhost"):
        raise SessionEmbeddingConfigError(
            "invalid_base_url",
            "an OpenAI compatible base url must not target localhost",
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return cleaned
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_private
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise SessionEmbeddingConfigError(
            "invalid_base_url",
            "an OpenAI compatible base url must not target a private, "
            "loopback, or link-local host",
        )
    return cleaned


class CRUDSessionEmbeddingSetting(CRUDBase[SessionEmbeddingSetting]):
    """CRUD operations for :class:`SessionEmbeddingSetting`."""

    def get_for_account(
        self, db: Session, *, account_id: Any
    ) -> Optional[SessionEmbeddingSetting]:
        """Return one account's setting, or ``None`` when it never opted in."""
        return (
            db.query(SessionEmbeddingSetting)
            .filter(SessionEmbeddingSetting.account_id == account_id)
            .one_or_none()
        )

    def get_or_create(
        self, db: Session, *, account_id: Any, commit: bool = False
    ) -> SessionEmbeddingSetting:
        """Return the account's setting, creating a disabled one if absent."""
        existing = self.get_for_account(db, account_id=account_id)
        if existing is not None:
            return existing
        setting = SessionEmbeddingSetting(
            account_id=account_id,
            enabled=False,
            provider=PROVIDER_OPENAI_COMPATIBLE,
            dimensions=EMBEDDING_DIMENSIONS,
        )
        db.add(setting)
        db.flush()
        if commit:
            db.commit()
            db.refresh(setting)
        return setting

    def enable(
        self,
        db: Session,
        *,
        account_id: Any,
        provider: str,
        model_identifier: str,
        base_url: Optional[str] = None,
        dimensions: int = EMBEDDING_DIMENSIONS,
        daily_cap_usd: Optional[float] = None,
        user_id: Optional[Any] = None,
        now: Optional[datetime] = None,
        commit: bool = False,
    ) -> SessionEmbeddingSetting:
        """Turn embedding on for one account, naming what it will talk to.

        Raises:
            SessionEmbeddingConfigError: The provider is unknown, the model is
                missing, an OpenAI compatible provider has no base url, the
                base url is not https or targets a private host, or the
                requested width is not the width the corpus column stores.
        """
        if provider not in EMBEDDING_PROVIDERS:
            raise SessionEmbeddingConfigError(
                "unknown_provider",
                f"provider must be one of {', '.join(EMBEDDING_PROVIDERS)}",
            )
        cleaned_model = (model_identifier or "").strip()
        if not cleaned_model:
            raise SessionEmbeddingConfigError(
                "model_required",
                "enabling embedding must name the model the text is sent to",
            )
        cleaned_base_url = (base_url or "").strip() or None
        if provider == PROVIDER_OPENAI_COMPATIBLE and not cleaned_base_url:
            raise SessionEmbeddingConfigError(
                "base_url_required",
                "an OpenAI compatible provider must name its base url",
            )
        if provider == PROVIDER_OPENAI_COMPATIBLE and cleaned_base_url:
            cleaned_base_url = validate_openai_compatible_base_url(cleaned_base_url)
        if provider == PROVIDER_LOCAL:
            # A local model runs in this process; a base url would be a lie
            # about where the text goes.
            cleaned_base_url = None
        if int(dimensions) != EMBEDDING_DIMENSIONS:
            raise SessionEmbeddingConfigError(
                "unsupported_dimensions",
                (
                    "the corpus stores vectors of "
                    f"{EMBEDDING_DIMENSIONS} dimensions; changing the width is "
                    "a migration, not a setting"
                ),
            )
        if daily_cap_usd is not None and float(daily_cap_usd) < 0:
            raise SessionEmbeddingConfigError(
                "invalid_daily_cap", "the daily cap cannot be negative"
            )

        setting = self.get_or_create(db, account_id=account_id)
        setting.enabled = True
        setting.provider = provider
        setting.model_identifier = cleaned_model
        setting.base_url = cleaned_base_url
        setting.dimensions = int(dimensions)
        setting.daily_cap_usd = (
            float(daily_cap_usd) if daily_cap_usd is not None else None
        )
        setting.enabled_at = now or datetime.now(UTC)
        setting.enabled_by_user_id = user_id
        # A fresh opt in starts clean: yesterday's cap is not today's state.
        setting.degraded_reason = None
        setting.degraded_at = None
        db.flush()
        if commit:
            db.commit()
            db.refresh(setting)
        return setting

    def disable(
        self, db: Session, *, account_id: Any, commit: bool = False
    ) -> Optional[SessionEmbeddingSetting]:
        """Turn embedding off, keeping the provider details for a re-enable."""
        setting = self.get_for_account(db, account_id=account_id)
        if setting is None:
            return None
        setting.enabled = False
        db.flush()
        if commit:
            db.commit()
            db.refresh(setting)
        return setting

    def mark_degraded(
        self,
        db: Session,
        *,
        account_id: Any,
        reason: str,
        now: Optional[datetime] = None,
        commit: bool = False,
    ) -> Optional[SessionEmbeddingSetting]:
        """Record why the last run did less than it wanted to.

        Degraded is not failed. The chunks stay pending, the setting stays
        enabled, and the reason is what a console or an operator reads to see
        that the backlog is waiting on a cap rather than broken.
        """
        setting = self.get_for_account(db, account_id=account_id)
        if setting is None:
            return None
        setting.degraded_reason = reason
        setting.degraded_at = now or datetime.now(UTC)
        db.flush()
        if commit:
            db.commit()
            db.refresh(setting)
        return setting

    def clear_degraded(
        self, db: Session, *, account_id: Any, commit: bool = False
    ) -> Optional[SessionEmbeddingSetting]:
        """Drop the degraded marker after a run that did its work."""
        setting = self.get_for_account(db, account_id=account_id)
        if setting is None or setting.degraded_reason is None:
            return setting
        setting.degraded_reason = None
        setting.degraded_at = None
        db.flush()
        if commit:
            db.commit()
            db.refresh(setting)
        return setting

    def enabled_account_ids(self, db: Session) -> list[str]:
        """Accounts that have opted in, for a sweeper that has no trigger."""
        rows = (
            db.query(SessionEmbeddingSetting.account_id)
            .filter(SessionEmbeddingSetting.enabled.is_(True))
            .all()
        )
        return [str(row[0]) for row in rows]

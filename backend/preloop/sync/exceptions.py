"""
Custom exceptions for preloop.sync.
"""


class PreloopSyncError(Exception):
    """Base exception for all Preloop Sync errors."""

    pass


class ConfigurationError(PreloopSyncError):
    """Raised when there's an issue with the configuration."""

    pass


class DatabaseError(PreloopSyncError):
    """Raised when there's an issue with the database operations."""

    pass


class TrackerError(PreloopSyncError):
    """Base exception for all tracker-related errors."""

    pass


class TrackerAuthenticationError(TrackerError):
    """Raised when there's an authentication issue with a tracker."""

    pass


class TrackerConnectionError(TrackerError):
    """Raised when there's a connection issue with a tracker."""

    pass


class TrackerRateLimitError(TrackerError):
    """Raised when a tracker API rate limit is hit."""

    pass


class TrackerResponseError(TrackerError):
    """Raised when a tracker API returns an error response.

    `status_code` carries the provider status when the raising call site knows
    it, so callers can branch on the response structurally instead of parsing
    the message. It stays None for the call sites that only have a message.
    """

    def __init__(self, *args: object, status_code: int | None = None) -> None:
        super().__init__(*args)
        self.status_code = status_code


class TrackerPermissionError(TrackerResponseError):
    """Provider explicitly denied permission, excluding throttling/auth failures."""


class EmbeddingError(PreloopSyncError):
    """Raised when there's an issue with generating embeddings."""

    pass

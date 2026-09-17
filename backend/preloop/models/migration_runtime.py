"""Lock policy for Alembic migrations that run against a live deployment.

The Helm ``pre-upgrade`` hook runs ``alembic upgrade head`` while the previous
API pods and sync workers keep serving traffic, so a migration competes for
locks with ordinary queries. Three settings keep that safe and are shared by
``alembic/env.py`` (which configures the migration context) and
``preloop.models.migrate`` (which drives the upgrade):

* one transaction per revision, so the ACCESS EXCLUSIVE lock an early revision
  takes on ``account`` is released when that revision commits instead of being
  held until the last revision of the batch finishes. Holding them all at once
  is what turns a routine upgrade into a deadlock: a request that already holds
  a share lock on ``user`` waits for a row lock on ``account`` held by the
  migration, while the migration waits for the exclusive lock on ``user``;
* a short per-session ``lock_timeout``, so a revision that cannot get its lock
  gives up in seconds rather than parking a lock request in front of every
  query that arrives behind it;
* a bounded retry loop, so the attempts that a short timeout makes cheap are
  actually retried instead of failing the release.

Every value has an environment override so an operator can tune a slow
migration without a new image.
"""

from __future__ import annotations

import os
import random
import re
import time
from typing import Any, Callable, Optional, Protocol, TypeVar

from sqlalchemy.exc import DBAPIError

# Postgres SQLSTATEs that mean "somebody else held the lock", not "this
# migration is wrong". Retrying the upgrade resumes from the last revision
# that committed, which is why per-revision transactions are a precondition
# for retrying at all.
#
# 57014 (query_canceled) is deliberately absent. A migration session sees it
# when the cluster ``statement_timeout`` fires, which means the revision is
# too slow rather than unlucky: it would fail the same way on all 20 attempts,
# each one holding ACCESS EXCLUSIVE for the full timeout with live traffic
# queued behind it. That revision needs a drained API, not a retry.
RETRYABLE_SQLSTATES = frozenset(
    {
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
        "55P03",  # lock_not_available (this is what lock_timeout raises)
    }
)

DEFAULT_LOCK_TIMEOUT = "5s"
DEFAULT_MAX_ATTEMPTS = 20
DEFAULT_RETRY_MIN_SECONDS = 3.0
DEFAULT_RETRY_MAX_SECONDS = 15.0

# ``5s``, ``250ms``, ``2min`` or a bare millisecond count. An integer with an
# optional unit and nothing else: ``lock_timeout`` is an integer GUC, and the
# value is spliced into a single libpq ``options`` token, so a fraction, an
# internal space or an overflowing count is refused at connect time and the
# Job dies before its first revision. Rejected here in favour of the default.
_LOCK_TIMEOUT_PATTERN = re.compile(r"^\d{1,9}(us|ms|s|min|h)?$")

T = TypeVar("T")


class _ConfigurableContext(Protocol):
    """The slice of ``alembic.context`` this module uses."""

    def configure(self, **kwargs: Any) -> None:
        """Configure the migration context (see ``alembic.context.configure``)."""


def _env_str(name: str, default: str) -> str:
    value = (os.getenv(name) or "").strip()
    return value or default


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def lock_timeout() -> str:
    """The per-session ``lock_timeout`` the migration connection asks for.

    Deliberately short. A migration that waits a minute for a lock also makes
    every query that arrives behind it wait a minute, because a pending
    ACCESS EXCLUSIVE request blocks the readers queued after it.
    """
    value = _env_str("PRELOOP_MIGRATION_LOCK_TIMEOUT", DEFAULT_LOCK_TIMEOUT)
    if not _LOCK_TIMEOUT_PATTERN.match(value):
        return DEFAULT_LOCK_TIMEOUT
    return value


def max_attempts() -> int:
    """How many times the whole ``upgrade head`` may be retried."""
    return _env_int("PRELOOP_MIGRATION_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)


def retry_delay_bounds() -> tuple[float, float]:
    """Inclusive (min, max) seconds of jittered backoff between attempts."""
    low = _env_float("PRELOOP_MIGRATION_RETRY_MIN_SECONDS", DEFAULT_RETRY_MIN_SECONDS)
    high = _env_float("PRELOOP_MIGRATION_RETRY_MAX_SECONDS", DEFAULT_RETRY_MAX_SECONDS)
    if high < low:
        high = low
    return low, high


def connect_args(url: str) -> dict[str, str]:
    """Connection options that pin ``lock_timeout`` for the migration session.

    Set at connect time through libpq ``options`` rather than with a ``SET``
    statement: the ``SET`` would have to run inside (and commit with) some
    transaction, and with one transaction per revision there is no longer an
    outer transaction to hang it on. Non-Postgres URLs get nothing.
    """
    if not url.startswith("postgresql"):
        return {}
    return {"options": f"-c lock_timeout={lock_timeout()}"}


def configure_online_context(
    context: _ConfigurableContext,
    *,
    connection: Any,
    target_metadata: Any,
) -> None:
    """Configure the online migration context with per-revision transactions."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # One transaction per revision: locks taken by revision N are released
        # before revision N+1 starts, and a retry resumes at the first revision
        # that did not commit.
        transaction_per_migration=True,
    )


def sqlstate_of(error: BaseException) -> Optional[str]:
    """Best-effort SQLSTATE for a driver error (psycopg 2 and 3 differ)."""
    original = getattr(error, "orig", error)
    for attribute in ("sqlstate", "pgcode"):
        code = getattr(original, attribute, None)
        if isinstance(code, str) and code:
            return code
    diagnostic = getattr(original, "diag", None)
    code = getattr(diagnostic, "sqlstate", None)
    return code if isinstance(code, str) and code else None


def is_lock_contention(error: BaseException) -> bool:
    """True when the failure is another session holding a lock, not bad DDL."""
    if not isinstance(error, DBAPIError):
        return False
    return sqlstate_of(error) in RETRYABLE_SQLSTATES


def run_with_lock_retries(
    operation: Callable[[], T],
    *,
    attempts: Optional[int] = None,
    delay_bounds: Optional[tuple[float, float]] = None,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Optional[Callable[[int, float, BaseException], None]] = None,
    jitter: Callable[[float, float], float] = random.uniform,
) -> T:
    """Run ``operation``, retrying only lock contention, with jittered backoff.

    Any other exception propagates on the first attempt: a broken revision must
    fail the release loudly instead of being retried twenty times.
    """
    total = attempts if attempts is not None else max_attempts()
    low, high = delay_bounds if delay_bounds is not None else retry_delay_bounds()
    for attempt in range(1, total + 1):
        try:
            return operation()
        # Exception, not BaseException: a cancelled or interrupted release must
        # stop, not sleep and try again.
        except Exception as error:
            if not is_lock_contention(error) or attempt == total:
                raise
            delay = jitter(low, high)
            if on_retry is not None:
                on_retry(attempt, delay, error)
            sleep(delay)
    raise AssertionError("unreachable: retry loop exited without a result")

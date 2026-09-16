"""Bind production session factories to the pytest transaction.

Production code opens short-lived sessions via ``get_session_factory()``.
Tests run inside one rolled-back transaction, so the factory must return
that session. The object still has to look like a real Session: GitLab
sets ``INIT_TEST_DATA=true``, ``TestClient`` lifespan calls
``create_test_data``, and that path does ``db = next(get_db_session());
db.query(...)`` rather than ``with factory() as db``. A contextmanager-only
stub then fails with ``Database setup failed``.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session


class BoundSession:
    """A sessionmaker() result that shares the test transaction.

    ``with factory() as db`` yields the real session and does not close it
    on exit. ``factory()`` itself still has Session methods so
    ``get_db_session`` / ``create_test_data`` can ``query`` and ``close``
    without rolling back the test transaction.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def __enter__(self) -> Session:
        return self._session

    def __exit__(self, *exc: object) -> bool:
        return False

    def close(self) -> None:
        return None

    def invalidate(self) -> None:
        return None

    def in_transaction(self) -> bool:
        return False

    def rollback(self) -> None:
        return None

    def __getattr__(self, name: str) -> object:
        return getattr(self._session, name)


def bound_session_factory(session: Session) -> Callable[[], BoundSession]:
    """Return a sessionmaker stand-in bound to ``session``."""

    return lambda: BoundSession(session)

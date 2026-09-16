"""The bound factory must look like a Session, not only a context manager.

GitLab sets INIT_TEST_DATA=true. TestClient lifespan then seeds via
``next(get_db_session()).query(...)``. A contextmanager-only stub made
that path raise Database setup failed for every test that used ``client``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from preloop.models.crud import crud_user
from preloop.models.db.session import get_db_session
from tests.bound_session import bound_session_factory


def test_get_db_session_yields_a_queryable_session(db_session, monkeypatch) -> None:
    """create_test_data's next(get_db_session()) path must see Session methods."""
    monkeypatch.setattr(
        "preloop.models.db.session.get_session_factory",
        lambda: bound_session_factory(db_session),
    )
    gen = get_db_session()
    db = next(gen)
    try:
        crud_user.get_by_email(db, email="nobody@example.com")
    finally:
        gen.close()
    assert db_session.is_active


def test_init_test_data_lifespan_does_not_fail_with_bound_factory(
    db_session, app, monkeypatch
) -> None:
    """The GitLab unit job's env that made test_flow_tree_stop ERROR at setup."""
    monkeypatch.setattr(
        "preloop.models.db.session.get_session_factory",
        lambda: bound_session_factory(db_session),
    )
    monkeypatch.setenv("INIT_TEST_DATA", "true")
    with TestClient(app):
        pass
    assert db_session.is_active

"""The polling worker must not hold locks while a tracker answers.

The scanner reads a project row, then calls the tracker's API for that
project's issues. The read opens a transaction; the call takes as long as the
remote side takes. Between them the worker sat "idle in transaction" for
minutes at a time, holding AccessShareLock on every table it had touched,
which is exactly the state that blocks an `ALTER TABLE` during an upgrade.
"""

from typing import Any, Dict, List
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from preloop.models.models import Organization, Project, Tracker
from preloop.sync.scanner.core import TrackerClient


@pytest.fixture
def tracker() -> Tracker:
    stub = MagicMock(spec=Tracker)
    stub.id = uuid4()
    stub.is_deleted = False
    stub.tracker_type = "github"
    stub.account_id = uuid4()
    return stub


@pytest.fixture
def organization() -> Organization:
    stub = MagicMock(spec=Organization)
    stub.id = 101
    stub.identifier = "test-org"
    stub.name = "Test Org"
    return stub


@pytest.fixture
def project() -> Project:
    stub = MagicMock(spec=Project)
    stub.id = 1001
    stub.identifier = "test-project"
    stub.name = "Test Project"
    return stub


def _client(tracker: Tracker, calls: Dict[str, Any], db_session: Session) -> Any:
    """A tracker client that records the transaction state of each call."""

    def record(name: str) -> Any:
        async def call(*args: Any, **kwargs: Any) -> List[Any]:
            calls[name] = db_session.in_transaction()
            return []

        return call

    internal = AsyncMock()
    internal.get_organizations = record("get_organizations")
    internal.get_projects = record("get_projects")
    internal.get_issues = record("get_issues")
    with patch("preloop.sync.trackers.github.GitHubTracker"):
        client = TrackerClient(tracker)
    client.client = internal
    return client


@pytest.mark.asyncio
async def test_issue_fetch_runs_outside_a_transaction(
    db_session: Session, tracker: Tracker, organization: Organization, project: Project
) -> None:
    calls: Dict[str, Any] = {}
    client = _client(tracker, calls, db_session)
    # Put the session in the state the worker is really in: it has just read
    # rows, so a transaction is open.
    db_session.execute(text("SELECT 1"))
    assert db_session.in_transaction() is True

    await client.scan_issues(db_session, organization, project)

    assert calls["get_issues"] is False


@pytest.mark.asyncio
async def test_project_and_organization_fetches_run_outside_a_transaction(
    db_session: Session, tracker: Tracker, organization: Organization
) -> None:
    calls: Dict[str, Any] = {}
    client = _client(tracker, calls, db_session)

    db_session.execute(text("SELECT 1"))
    await client.scan_organizations(db_session)
    assert calls["get_organizations"] is False

    db_session.execute(text("SELECT 1"))
    await client.scan_projects(db_session, organization)
    assert calls["get_projects"] is False

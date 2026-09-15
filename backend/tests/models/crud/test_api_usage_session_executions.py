"""The session to execution link the note scope is keyed on (#637).

A runtime session has no execution column: the link is the governed usage the
run produced, so "which runs is this session carrying" is a question about
``ApiUsage``. The note scope asks it on every refused note, which is why this
helper is bounded, account scoped and newest first.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from preloop.models import models
from preloop.models.crud import crud_api_usage
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.runtime_session import RuntimeSession


def _session(db_session, account_id) -> RuntimeSession:
    session = RuntimeSession(
        id=uuid4(),
        account_id=account_id,
        session_source_type="claude_code",
        session_source_id=f"scope-{uuid4()}",
        session_reference="/repo",
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    db_session.flush()
    return session


def _execution(db_session, account_id):
    flow = models.Flow(
        account_id=account_id,
        name="Scoped flow",
        prompt_template="Example",
        agent_config={},
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(flow_id=flow.id)
    db_session.add(execution)
    db_session.flush()
    return execution


def _usage(db_session, account_id, execution, session, when):
    db_session.add(
        ApiUsage(
            account_id=account_id,
            endpoint="/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=0.2,
            flow_execution_id=execution.id,
            runtime_session_id=session.id,
            timestamp=when,
        )
    )
    db_session.flush()


def test_executions_are_distinct_newest_first_and_bounded(
    db_session, create_account
) -> None:
    """One row per run, most recent first, never more than asked for."""
    account = create_account()
    session = _session(db_session, account.id)
    base = datetime(2026, 6, 1, 12, 0)

    older = _execution(db_session, account.id)
    newer = _execution(db_session, account.id)
    _usage(db_session, account.id, older, session, base)
    # A second call by the same run must not produce a second entry.
    _usage(db_session, account.id, older, session, base + timedelta(minutes=1))
    _usage(db_session, account.id, newer, session, base + timedelta(minutes=2))

    assert crud_api_usage.list_execution_ids_for_session(
        db_session, account_id=account.id, runtime_session_id=session.id
    ) == [newer.id, older.id]

    assert crud_api_usage.list_execution_ids_for_session(
        db_session,
        account_id=account.id,
        runtime_session_id=session.id,
        limit=1,
    ) == [newer.id]


def test_a_session_id_alone_does_not_cross_an_account(
    db_session, create_account
) -> None:
    """The scope check reads this, so the account filter is load bearing."""
    account = create_account()
    other = create_account()
    session = _session(db_session, account.id)
    execution = _execution(db_session, account.id)
    _usage(db_session, account.id, execution, session, datetime(2026, 6, 1, 12, 0))

    assert (
        crud_api_usage.list_execution_ids_for_session(
            db_session, account_id=other.id, runtime_session_id=session.id
        )
        == []
    )


def test_a_session_with_no_governed_call_has_no_runs(
    db_session, create_account
) -> None:
    """Nothing to walk, which the scope model reads as no lineage."""
    account = create_account()
    session = _session(db_session, account.id)

    assert (
        crud_api_usage.list_execution_ids_for_session(
            db_session, account_id=account.id, runtime_session_id=session.id
        )
        == []
    )

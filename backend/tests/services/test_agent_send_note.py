"""The ``send_note`` builtin tool: an agent noting another agent.

The delivery half of this shipped with operator notes, so these tests pin the
half that is new: who the author is, which targets that author can reach, what
happens when the call is malformed, and that the record and the ceiling are
the same ones a human's note answers to.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from preloop.models.crud import (
    crud_account,
    crud_agent_control_command,
    crud_audit_log,
    crud_managed_agent,
)
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.runtime_session import RuntimeSession
from preloop.services import agent_send_note, operator_notes
from preloop.services.agent_send_note import send_note_from_agent


@pytest.fixture
def account(db_session):
    return crud_account.create(
        db_session,
        obj_in={"organization_name": "Note Author Org", "is_active": True},
    )


@pytest.fixture
def other_account(db_session):
    return crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Org", "is_active": True},
    )


@pytest.fixture
def author(db_session, account):
    return crud_managed_agent.create_custom_agent(
        db_session,
        account_id=account.id,
        display_name="Reviewer",
        commit=True,
    )


@pytest.fixture
def target(db_session, account):
    return crud_managed_agent.create_custom_agent(
        db_session,
        account_id=account.id,
        display_name="Implementer",
        commit=True,
    )


def _session_for(db_session, account, agent=None, *, source_id="workspace-1"):
    session = RuntimeSession(
        id=uuid4(),
        account_id=account.id,
        session_source_type="claude_code",
        session_source_id=source_id,
        session_reference="/repo",
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    if agent is not None:
        agent.runtime_session_id = session.id
    db_session.flush()
    return session


# --- the account boundary ---------------------------------------------------


def test_an_agent_cannot_address_a_target_in_another_account(
    db_session, account, other_account, author
):
    """Account A's agent cannot reach account B's agent, and writes nothing."""
    foreign = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=other_account.id,
        display_name="Their Worker",
        commit=True,
    )

    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="leak",
        agent_id=str(foreign.id),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == agent_send_note.ERROR_TARGET_NOT_FOUND
    assert (
        crud_agent_control_command.list_notes(
            db_session, account_id=other_account.id, managed_agent_id=foreign.id
        )
        == []
    )


def test_a_foreign_runtime_session_is_not_reachable_either(
    db_session, account, other_account, author
):
    """The session path is scoped to the account too, not just the agent one."""
    foreign_session = _session_for(
        db_session, other_account, source_id="their-workspace"
    )

    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="leak",
        runtime_session_id=str(foreign_session.id),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == agent_send_note.ERROR_TARGET_NOT_FOUND
    assert (
        crud_agent_control_command.list_notes(
            db_session,
            account_id=other_account.id,
            runtime_session_id=foreign_session.id,
        )
        == []
    )


# --- structured refusals ----------------------------------------------------


def test_zero_targets_is_a_structured_refusal_and_writes_nothing(
    db_session, account, author, target
):
    """Naming no target is an answer the model can correct, not an exception."""
    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="Ship it.",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == agent_send_note.ERROR_TARGET_COUNT
    assert "exactly one" in result["error"]["message"]
    assert "named 0" in result["error"]["message"]
    assert _notes_written(db_session, account) == 0


def test_two_targets_is_a_structured_refusal_naming_both(
    db_session, account, author, target
):
    """Two targets name both, so the caller knows what to drop."""
    session = _session_for(db_session, account)

    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="Ship it.",
        agent_id=str(target.id),
        runtime_session_id=str(session.id),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == agent_send_note.ERROR_TARGET_COUNT
    assert "agent_id" in result["error"]["message"]
    assert "runtime_session_id" in result["error"]["message"]
    assert _notes_written(db_session, account) == 0


def test_a_call_with_no_agent_identity_is_refused(db_session, account, target):
    """An unattributed note has no author, so it must not be deliverable."""
    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=None,
        text="Ship it.",
        agent_id=str(target.id),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == agent_send_note.ERROR_NO_AUTHOR
    assert _notes_written(db_session, account) == 0


def test_an_author_from_another_account_is_refused(
    db_session, account, other_account, target
):
    """The author is resolved in the account the call carries, and nowhere else."""
    foreign_author = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=other_account.id,
        display_name="Their Reviewer",
        commit=True,
    )

    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=foreign_author.id,
        text="Ship it.",
        agent_id=str(target.id),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == agent_send_note.ERROR_NO_AUTHOR
    assert _notes_written(db_session, account) == 0


def test_an_empty_or_oversized_body_is_refused(db_session, account, author, target):
    """The same bounds the API schema enforces, as refusals rather than 422s."""
    empty = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="   ",
        agent_id=str(target.id),
    )
    assert empty["error"]["code"] == agent_send_note.ERROR_EMPTY_BODY

    huge = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="x" * (operator_notes.MAX_NOTE_BODY_CHARS + 1),
        agent_id=str(target.id),
    )
    assert huge["error"]["code"] == agent_send_note.ERROR_BODY_TOO_LONG
    assert _notes_written(db_session, account) == 0


def test_a_malformed_target_id_is_refused_not_raised(db_session, account, author):
    """A id that is not an id is the caller's mistake, not a database error."""
    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="Ship it.",
        agent_id="the-other-one",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == agent_send_note.ERROR_TARGET_COUNT
    assert _notes_written(db_session, account) == 0


# --- targets ----------------------------------------------------------------


def _execution_on_session(db_session, account, session):
    """One flow execution whose newest governed call names *session*."""
    from preloop.models import models

    flow = models.Flow(
        account_id=account.id,
        name="Example flow",
        prompt_template="Example",
        agent_config={},
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(flow_id=flow.id)
    db_session.add(execution)
    db_session.flush()
    db_session.add(
        ApiUsage(
            account_id=account.id,
            endpoint="/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=0.2,
            flow_execution_id=execution.id,
            runtime_session_id=session.id,
            timestamp=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    db_session.flush()
    return execution


def test_a_session_target_reaches_that_session_only(db_session, account, author):
    """A session with no agent behind it is still a target."""
    session = _session_for(db_session, account, source_id="credential-flow")

    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="Only you.",
        runtime_session_id=str(session.id),
    )

    assert result["ok"] is True
    assert result["note"]["runtime_session_id"] == str(session.id)
    assert result["note"]["managed_agent_id"] is None


def test_an_execution_target_resolves_to_the_session_it_runs_on(
    db_session, account, author, target
):
    """The execution path is the same resolver the REST route uses."""
    session = _session_for(db_session, account, target)
    execution = _execution_on_session(db_session, account, session)

    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="Your base branch moved.",
        execution_id=str(execution.id),
    )

    assert result["ok"] is True
    assert result["note"]["runtime_session_id"] == str(session.id)
    assert result["note"]["managed_agent_id"] == str(target.id)


def test_an_execution_with_no_governed_call_yet_is_refused(db_session, account, author):
    """Nothing to steer until the run has opened a session."""
    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="too early",
        execution_id=str(uuid4()),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == agent_send_note.ERROR_TARGET_NOT_FOUND
    assert "runtime session" in result["error"]["message"]
    assert _notes_written(db_session, account) == 0


# --- the author -------------------------------------------------------------


def test_the_stored_author_is_the_agent_with_an_agent_credential(
    db_session, account, author, target
):
    """The row names the agent, and the credential kind is not a user's."""
    session = _session_for(db_session, account, target)

    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="The migration needs a downgrade before you push.",
        agent_id=str(target.id),
    )

    assert result["ok"] is True
    assert result["note"]["author"]["agent_id"] == str(author.id)
    assert result["note"]["author"]["auth_method"] == operator_notes.AUTH_METHOD_AGENT
    assert result["note"]["author"]["display"] == "Reviewer (agent)"
    assert result["note"]["state"] == "pending"
    assert result["note"]["runtime_session_id"] == str(session.id)

    stored = crud_agent_control_command.get_note(
        db_session, account_id=account.id, note_id=result["note"]["note_id"]
    )
    assert stored.created_by_managed_agent_id == author.id
    assert stored.created_by_user_id is None
    assert stored.author_auth_method == operator_notes.AUTH_METHOD_AGENT
    assert stored.source == agent_send_note.NOTE_SOURCE_AGENT_TOOL


def test_the_author_is_never_taken_from_the_arguments(
    db_session, account, author, target
):
    """A note is signed by the identity that called, not by what it claimed."""
    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="Signed by whom?",
        agent_id=str(target.id),
    )

    stored = crud_agent_control_command.get_note(
        db_session, account_id=account.id, note_id=result["note"]["note_id"]
    )
    # The target is the only agent the caller named; the author is the caller.
    assert stored.managed_agent_id == target.id
    assert stored.created_by_managed_agent_id == author.id


# --- delivery through the existing rail -------------------------------------


def test_the_note_is_delivered_to_the_targets_next_turn(
    db_session, account, author, target
):
    """Agent authored notes ride the rail human notes already ride."""
    session = _session_for(db_session, account, target)
    send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="Rebase before you push.",
        agent_id=str(target.id),
    )

    messages = [{"role": "user", "content": "carry on"}]
    payload = {"messages": messages}
    delivered = operator_notes.deliver_gateway_notes(
        db_session,
        account_id=str(account.id),
        managed_agent_id=str(target.id),
        runtime_session_id=str(session.id),
        protocol=operator_notes.PROTOCOL_OPENAI_CHAT,
        payload=payload,
        messages=messages,
    )

    assert len(delivered) == 1
    block = messages[-1]["content"]
    assert "<operator-notes" in block
    assert "Rebase before you push." in block
    assert 'from="Reviewer (agent)"' in block
    assert f'auth="{operator_notes.AUTH_METHOD_AGENT}"' in block

    stored = crud_agent_control_command.get_note(
        db_session, account_id=account.id, note_id=delivered[0].command_id
    )
    assert operator_notes.note_state(stored) == "delivered"
    assert stored.delivery_channel == operator_notes.CHANNEL_GATEWAY


def test_the_stored_envelope_is_the_same_a2a_shape(db_session, account, author, target):
    """No second envelope for agent authors: the same one, different author."""
    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="Check the flaky test first.",
        agent_id=str(target.id),
    )

    stored = crud_agent_control_command.get_note(
        db_session, account_id=account.id, note_id=result["note"]["note_id"]
    )
    envelope = stored.envelope
    assert envelope["kind"] == "message"
    assert envelope["parts"] == [
        {"kind": "text", "text": "Check the flaky test first."}
    ]
    assert envelope["metadata"]["preloop.ai/kind"] == "operator_note"
    author_meta = envelope["metadata"]["preloop.ai/author"]
    assert author_meta["userId"] is None
    assert author_meta["authMethod"] == operator_notes.AUTH_METHOD_AGENT


# --- the rate limit ---------------------------------------------------------


def test_the_per_author_rate_limit_applies_to_agent_authors(
    db_session, account, author, target, monkeypatch
):
    """The same ceiling, keyed on the agent that wrote the notes."""
    monkeypatch.setattr(operator_notes, "NOTE_RATE_LIMIT_PER_HOUR", 3)

    for index in range(3):
        allowed = send_note_from_agent(
            db_session,
            account_id=str(account.id),
            author_agent_id=author.id,
            text=f"note {index}",
            agent_id=str(target.id),
        )
        assert allowed["ok"] is True

    refused = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="one more",
        agent_id=str(target.id),
    )

    assert refused["ok"] is False
    assert refused["error"]["code"] == agent_send_note.ERROR_RATE_LIMITED
    assert "Rate limit reached" in refused["error"]["message"]
    assert _notes_written(db_session, account) == 3


def test_the_rate_limit_is_per_author_not_per_account(
    db_session, account, author, target, monkeypatch
):
    """One noisy agent does not spend another agent's budget."""
    monkeypatch.setattr(operator_notes, "NOTE_RATE_LIMIT_PER_HOUR", 2)
    second_author = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=account.id,
        display_name="Second Reviewer",
        commit=True,
    )

    for index in range(2):
        send_note_from_agent(
            db_session,
            account_id=str(account.id),
            author_agent_id=author.id,
            text=f"note {index}",
            agent_id=str(target.id),
        )
    assert (
        send_note_from_agent(
            db_session,
            account_id=str(account.id),
            author_agent_id=author.id,
            text="over",
            agent_id=str(target.id),
        )["ok"]
        is False
    )

    assert (
        send_note_from_agent(
            db_session,
            account_id=str(account.id),
            author_agent_id=second_author.id,
            text="mine",
            agent_id=str(target.id),
        )["ok"]
        is True
    )


# --- the record -------------------------------------------------------------


def test_one_audit_row_per_call_names_the_agent_as_actor(
    db_session, account, author, target
):
    """The act is recorded before the caller is told it worked."""
    before = datetime.now(UTC) - timedelta(minutes=1)

    result = send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="Hold the deploy.",
        agent_id=str(target.id),
    )

    rows = [
        row
        for row in crud_audit_log.get_by_account(
            db_session, account_id=account.id, limit=50
        )
        if row.action == operator_notes.AUDIT_NOTE_SENT
        and row.timestamp.replace(tzinfo=UTC) >= before
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row.resource_type == "operator_note"
    assert row.resource_id == result["note"]["note_id"]
    assert row.status == "success"
    # No user invented for an agent's act; the actor is named in the details.
    assert row.user_id is None
    assert row.details["actor_type"] == "managed_agent"
    assert row.details["actor_managed_agent_id"] == str(author.id)
    assert row.details["managed_agent_id"] == str(target.id)
    assert row.details["author_auth_method"] == operator_notes.AUTH_METHOD_AGENT


def test_a_refused_call_writes_no_audit_row(db_session, account, author):
    """A refusal is not an act, so there is nothing to record."""
    before = datetime.now(UTC) - timedelta(minutes=1)

    send_note_from_agent(
        db_session,
        account_id=str(account.id),
        author_agent_id=author.id,
        text="nowhere",
    )

    rows = [
        row
        for row in crud_audit_log.get_by_account(
            db_session, account_id=account.id, limit=50
        )
        if row.action == operator_notes.AUDIT_NOTE_SENT
        and row.timestamp.replace(tzinfo=UTC) >= before
    ]
    assert rows == []


def _notes_written(db_session, account) -> int:
    """Every note row in this account, whatever its target."""
    from preloop.models.models.agent_control_command import AgentControlCommand

    return (
        db_session.query(AgentControlCommand)
        .filter(
            AgentControlCommand.account_id == account.id,
            AgentControlCommand.kind == "note",
        )
        .count()
    )

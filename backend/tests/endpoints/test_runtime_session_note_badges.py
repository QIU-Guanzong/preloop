"""The sessions list says which sessions were noted, and by whom last.

Attribution stops being cosmetic once an agent can write a note: a note that
changed a run and looks like it came from a person, when another agent wrote
it, is a misleading audit surface. These fields ride the list row on purpose,
so the console never asks a second time per row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from preloop.models.crud import (
    crud_agent_control_command,
    crud_api_usage,
    crud_managed_agent,
    crud_runtime_session,
)


def _session(db_session, test_user, *, source_id: str):
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="claude_code",
        session_source_id=source_id,
        session_reference=f"claude-{source_id}",
        runtime_principal_type="claude_code",
        runtime_principal_id=source_id,
        runtime_principal_name="Claude Workspace",
        started_at=test_user.created_at,
        last_activity_at=test_user.created_at,
    )
    db_session.commit()
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        runtime_session_id=str(session.id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=10,
        completion_tokens=4,
        total_tokens=14,
        estimated_cost=0.01,
        runtime_principal_type="claude_code",
        runtime_principal_id=source_id,
        runtime_principal_name="Claude Workspace",
    )
    db_session.commit()
    return session


def _note(
    db_session,
    test_user,
    agent,
    session,
    *,
    author_display: str,
    author_auth_method: str,
    created_at: datetime | None = None,
):
    note_id = uuid4().hex[:16]
    note = crud_agent_control_command.create_note(
        db_session,
        account_id=test_user.account_id,
        managed_agent_id=agent.id,
        runtime_session_id=session.id,
        note_id=note_id,
        body="Rebase before you push.",
        envelope={"kind": "message", "messageId": note_id},
        author_display=author_display,
        author_auth_method=author_auth_method,
        created_by_user_id=None,
        expires_at=None,
    )
    if created_at is not None:
        note.created_at = created_at.replace(tzinfo=None)
        db_session.commit()
    return note


def test_sessions_list_carries_note_count_and_newest_author(
    client, db_session, test_user
):
    """A noted session names its newest author and how many notes it holds."""
    agent = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=test_user.account_id,
        display_name="Noted Agent",
        commit=True,
    )
    noted = _session(db_session, test_user, source_id="workspace-noted")
    quiet = _session(db_session, test_user, source_id="workspace-quiet")
    now = datetime.now(UTC)
    _note(
        db_session,
        test_user,
        agent,
        noted,
        author_display="Jane Doe",
        author_auth_method="jwt",
        created_at=now - timedelta(minutes=10),
    )
    _note(
        db_session,
        test_user,
        agent,
        noted,
        author_display="Reviewer",
        author_auth_method="agent",
        created_at=now - timedelta(minutes=1),
    )

    response = client.get("/api/v1/runtime-sessions")

    assert response.status_code == 200
    items = {item["id"]: item for item in response.json()["items"]}
    noted_row = items[str(noted.id)]
    assert noted_row["note_count"] == 2
    assert noted_row["latest_note_author_display"] == "Reviewer"
    assert noted_row["latest_note_author_auth_method"] == "agent"
    assert noted_row["latest_note_at"] is not None

    quiet_row = items[str(quiet.id)]
    assert quiet_row["note_count"] == 0
    assert quiet_row["latest_note_author_display"] is None
    assert quiet_row["latest_note_author_auth_method"] is None
    assert quiet_row["latest_note_at"] is None


def test_sessions_list_reads_notes_once_for_the_whole_page(
    client, db_session, test_user, monkeypatch
):
    """One note lookup per page: the console must not pay per row."""
    from preloop.services import runtime_session_explorer as rse_mod

    agent = crud_managed_agent.create_custom_agent(
        db_session,
        account_id=test_user.account_id,
        display_name="Busy Agent",
        commit=True,
    )
    sessions = [
        _session(db_session, test_user, source_id=f"workspace-page-{index}")
        for index in range(3)
    ]
    for session in sessions:
        _note(
            db_session,
            test_user,
            agent,
            session,
            author_display="Reviewer",
            author_auth_method="agent",
        )

    calls: list[list[str]] = []
    original = rse_mod.crud_agent_control_command.note_summaries_for_sessions

    def counting(db, *, account_id, runtime_session_ids):
        ids = list(runtime_session_ids)
        calls.append(ids)
        return original(db, account_id=account_id, runtime_session_ids=ids)

    monkeypatch.setattr(
        rse_mod.crud_agent_control_command,
        "note_summaries_for_sessions",
        counting,
    )

    response = client.get("/api/v1/runtime-sessions")

    assert response.status_code == 200
    assert len(calls) == 1
    assert len(calls[0]) == len(response.json()["items"])

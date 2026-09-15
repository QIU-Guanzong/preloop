"""Coalescing must not load every active JSONB payload for a flow."""

import uuid

from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
from preloop.models.crud.flow_execution import tracker_object_payload_match
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate


def _make_account(db: Session):
    account = crud_account.create(
        db,
        obj_in={
            "organization_name": f"obj-lookup-{uuid.uuid4().hex[:8]}",
            "is_active": True,
            "meta_data": {},
        },
    )
    db.commit()
    db.refresh(account)
    return account


def _make_flow(db: Session, account_id):
    flow = crud_flow.create(
        db=db,
        flow_in=FlowCreate(
            name=f"obj-lookup-{uuid.uuid4().hex[:8]}",
            prompt_template="hello",
            trigger_event_source="github",
            trigger_event_types=["issue_labeled"],
            agent_type="openhands",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            is_enabled=True,
            account_id=account_id,
        ),
        account_id=account_id,
    )
    db.commit()
    db.refresh(flow)
    return flow


def _running(db: Session, flow_id, *, details: dict):
    execution = crud_flow_execution.create(
        db,
        obj_in=FlowExecutionCreate(
            flow_id=flow_id,
            status="RUNNING",
            trigger_event_details=details,
        ),
    )
    db.commit()
    db.refresh(execution)
    return execution


def _github_issue_details(number: int) -> dict:
    return {
        "source": "github",
        "payload": {
            "action": "labeled",
            "issue": {"number": number},
            "repository": {"full_name": "example-org/example-repo"},
        },
    }


def test_tracker_object_payload_match_parses_github_and_gitlab() -> None:
    assert (
        tracker_object_payload_match("github:example-org/example-repo:issue:626")
        is not None
    )
    assert (
        tracker_object_payload_match("github:example-org/example-repo:pr:700")
        is not None
    )
    assert (
        tracker_object_payload_match("gitlab:example-group/example-project:issue:23")
        is not None
    )
    assert tracker_object_payload_match("not-a-key") is None


def test_get_running_by_flow_filters_tracker_object(db_session: Session) -> None:
    account = _make_account(db_session)
    flow = _make_flow(db_session, account.id)
    match = _running(db_session, flow.id, details=_github_issue_details(626))
    other = _running(db_session, flow.id, details=_github_issue_details(999))

    all_active = crud_flow_execution.get_running_by_flow(
        db_session, flow_id=flow.id, account_id=account.id
    )
    assert {row.id for row in all_active} == {match.id, other.id}

    narrowed = crud_flow_execution.get_running_by_flow(
        db_session,
        flow_id=flow.id,
        account_id=account.id,
        tracker_object_key="github:example-org/example-repo:issue:626",
    )
    assert [row.id for row in narrowed] == [match.id]
    assert narrowed[0].status == "RUNNING"
    assert narrowed[0].trigger_event_details["payload"]["issue"]["number"] == 626

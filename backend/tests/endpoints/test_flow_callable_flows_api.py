"""The ``callable_flows`` allowlist over the flow endpoints (#627).

Readable and writable on create and update, rejected on write when an operator
cannot have meant it. Nothing enforces the list at call time yet, so what these
tests pin is the contract: what round trips, and what is refused with a reason
that names the entry.
"""

import uuid

import pytest

from preloop.models import models
from preloop.models.crud import crud_flow
from preloop.models.schemas.flow import FlowCreate

FLOWS_URL = "/api/v1/flows"


def _flow_body(name: str, **extra) -> dict:
    body = {
        "name": name,
        "prompt_template": "t",
        "agent_type": "codex",
        "agent_config": {},
        "allowed_mcp_servers": [],
        "allowed_mcp_tools": [],
    }
    body.update(extra)
    return body


@pytest.fixture
def child_flow(db_session, test_user) -> models.Flow:
    """A flow in the caller's account, the legitimate delegation target."""
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Child Flow",
            prompt_template="t",
            agent_type="codex",
            agent_config={},
        ),
        account_id=test_user.account_id,
    )
    db_session.flush()
    return flow


@pytest.fixture
def foreign_flow(db_session) -> models.Flow:
    """A flow owned by another account, which must never be callable."""
    other = models.Account(organization_name=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    db_session.flush()
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Foreign Flow",
            prompt_template="t",
            agent_type="codex",
            agent_config={},
        ),
        account_id=other.id,
    )
    db_session.flush()
    return flow


def _unique(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


# --- round trip ------------------------------------------------------------


def test_a_valid_allowlist_round_trips_unchanged(client, child_flow):
    """What was written is what is read back, on the create and on the read."""
    entry = {
        "flow": "Child Flow",
        "max_children": 3,
        "max_usd_per_child": 1.5,
        "allow_self": False,
    }
    created = client.post(
        FLOWS_URL, json=_flow_body(_unique("Parent"), callable_flows=[entry])
    )
    assert created.status_code == 200, created.text
    assert created.json()["callable_flows"] == [entry]

    read = client.get(f"{FLOWS_URL}/{created.json()['id']}")
    assert read.status_code == 200, read.text
    assert read.json()["callable_flows"] == [entry]


def test_an_allowlist_is_writable_on_update_and_revocable(client, child_flow):
    """An operator has to be able to grant and then take the list away."""
    created = client.post(FLOWS_URL, json=_flow_body(_unique("Parent")))
    assert created.status_code == 200, created.text
    flow_id = created.json()["id"]
    assert created.json()["callable_flows"] is None

    granted = client.put(
        f"{FLOWS_URL}/{flow_id}",
        json={"callable_flows": [{"flow": "Child Flow", "max_children": 1}]},
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["callable_flows"][0]["flow"] == "Child Flow"
    assert granted.json()["callable_flows"][0]["max_children"] == 1

    revoked = client.put(f"{FLOWS_URL}/{flow_id}", json={"callable_flows": []})
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["callable_flows"] == []


def test_an_absent_allowlist_round_trips_as_no_delegation(client):
    """The default is null, which is "may call nothing"."""
    created = client.post(FLOWS_URL, json=_flow_body(_unique("Parent")))
    assert created.status_code == 200, created.text
    assert created.json()["callable_flows"] is None

    empty = client.post(
        FLOWS_URL, json=_flow_body(_unique("Parent"), callable_flows=[])
    )
    assert empty.status_code == 200, empty.text
    assert empty.json()["callable_flows"] == []


# --- rejections, one per case ----------------------------------------------


def test_an_entry_in_another_account_is_rejected(client, foreign_flow):
    """4xx, and the reason names the entry."""
    response = client.post(
        FLOWS_URL,
        json=_flow_body(_unique("Parent"), callable_flows=[{"flow": "Foreign Flow"}]),
    )
    assert response.status_code == 422, response.text
    assert "Foreign Flow" in response.text


def test_an_entry_in_another_account_is_rejected_on_update(client, foreign_flow):
    """The update path is the one an operator uses to widen an allowlist."""
    created = client.post(FLOWS_URL, json=_flow_body(_unique("Parent")))
    assert created.status_code == 200, created.text

    response = client.put(
        f"{FLOWS_URL}/{created.json()['id']}",
        json={"callable_flows": [{"flow": "Foreign Flow"}]},
    )
    assert response.status_code == 422, response.text
    assert "Foreign Flow" in response.text


def test_a_self_reference_is_rejected_without_the_flag(client):
    """A flow may not call itself by accident."""
    name = _unique("Parent")
    response = client.post(
        FLOWS_URL, json=_flow_body(name, callable_flows=[{"flow": name}])
    )
    assert response.status_code == 422, response.text
    assert name in response.text
    assert "allow_self" in response.text


def test_a_self_reference_is_accepted_with_the_flag(client):
    """With the flag set it is a deliberate choice, so it is stored."""
    name = _unique("Parent")
    response = client.post(
        FLOWS_URL,
        json=_flow_body(name, callable_flows=[{"flow": name, "allow_self": True}]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["callable_flows"] == [
        {
            "flow": name,
            "max_children": None,
            "max_usd_per_child": None,
            "allow_self": True,
        }
    ]


def test_a_duplicate_entry_is_rejected(client, child_flow):
    """One flow, one set of ceilings: a second entry hides the first."""
    response = client.post(
        FLOWS_URL,
        json=_flow_body(
            _unique("Parent"),
            callable_flows=[
                {"flow": "Child Flow", "max_children": 1},
                {"flow": "Child Flow", "max_children": 9},
            ],
        ),
    )
    assert response.status_code == 422, response.text
    assert "duplicate entry for 'Child Flow'" in response.text


def test_a_negative_ceiling_is_rejected(client, child_flow):
    """A negative budget is not a budget."""
    response = client.post(
        FLOWS_URL,
        json=_flow_body(
            _unique("Parent"),
            callable_flows=[{"flow": "Child Flow", "max_usd_per_child": -1}],
        ),
    )
    assert response.status_code == 422, response.text
    assert "max_usd_per_child must be" in response.text
    assert "Child Flow" in response.text


def test_a_zero_child_ceiling_is_rejected(client, child_flow):
    """Zero children is expressed by leaving the flow off the list."""
    response = client.post(
        FLOWS_URL,
        json=_flow_body(
            _unique("Parent"),
            callable_flows=[{"flow": "Child Flow", "max_children": 0}],
        ),
    )
    assert response.status_code == 422, response.text
    assert "max_children must be" in response.text


def test_an_unknown_key_is_rejected(client, child_flow):
    """A misspelled ceiling is a ceiling that does not apply, so refuse it."""
    response = client.post(
        FLOWS_URL,
        json=_flow_body(
            _unique("Parent"),
            callable_flows=[{"flow": "Child Flow", "max_cost": 5}],
        ),
    )
    assert response.status_code == 422, response.text
    assert "max_cost" in response.text

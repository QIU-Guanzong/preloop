"""Account scoped validation of the ``callable_flows`` allowlist (#627).

Every rejection here is a rejection an operator has to be able to act on, so
each test asserts the message names the entry that caused it.
"""

import uuid

import pytest

from preloop.models import models
from preloop.models.crud import crud_flow
from preloop.models.schemas.flow import CallableFlowEntry, FlowCreate
from preloop.services.flow_delegation import (
    CallableFlowsError,
    resolve_callable_flow,
    validate_callable_flows,
)


def _flow(db_session, *, name: str, account_id) -> models.Flow:
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=name,
            prompt_template="t",
            agent_type="codex",
            agent_config={},
        ),
        account_id=account_id,
    )


@pytest.fixture
def child_flow(db_session, test_user) -> models.Flow:
    """A flow in the calling account, the legitimate delegation target."""
    return _flow(db_session, name="Child Flow", account_id=test_user.account_id)


@pytest.fixture
def other_account_flow(db_session) -> models.Flow:
    """A flow owned by a different account."""
    other = models.Account(organization_name=f"other-{uuid.uuid4().hex[:8]}")
    db_session.add(other)
    db_session.flush()
    return _flow(db_session, name="Foreign Flow", account_id=other.id)


# --- resolution ------------------------------------------------------------


def test_a_reference_resolves_inside_the_account(db_session, test_user, child_flow):
    """A name, in any case, resolves to the account's own flow."""
    assert (
        resolve_callable_flow(
            db_session, reference="Child Flow", account_id=test_user.account_id
        ).id
        == child_flow.id
    )
    assert (
        resolve_callable_flow(
            db_session, reference="child flow", account_id=test_user.account_id
        ).id
        == child_flow.id
    )


def test_a_reference_never_resolves_across_accounts(
    db_session, test_user, other_account_flow
):
    """Resolution is the account boundary: another account's flow is invisible."""
    assert (
        resolve_callable_flow(
            db_session, reference="Foreign Flow", account_id=test_user.account_id
        )
        is None
    )


# --- validation ------------------------------------------------------------


def test_null_and_empty_both_validate_to_no_delegation(db_session, test_user):
    """Revoking an allowlist must never be the write that fails."""
    assert (
        validate_callable_flows(
            db_session, callable_flows=None, account_id=test_user.account_id
        )
        == []
    )
    assert (
        validate_callable_flows(
            db_session, callable_flows=[], account_id=test_user.account_id
        )
        == []
    )


def test_a_valid_entry_validates(db_session, test_user, child_flow):
    """The happy path returns the parsed entries."""
    entries = validate_callable_flows(
        db_session,
        callable_flows=[{"flow": "Child Flow", "max_children": 2}],
        account_id=test_user.account_id,
        flow_name="Parent Flow",
    )
    assert [entry.flow for entry in entries] == ["Child Flow"]


def test_an_entry_from_another_account_is_rejected(
    db_session, test_user, other_account_flow
):
    """A flow the account does not own is not a delegation target."""
    with pytest.raises(CallableFlowsError) as error:
        validate_callable_flows(
            db_session,
            callable_flows=[{"flow": "Foreign Flow"}],
            account_id=test_user.account_id,
            flow_name="Parent Flow",
        )
    assert "Foreign Flow" in str(error.value)


def test_an_unknown_reference_is_rejected(db_session, test_user):
    """A typo must not be stored as a permission nobody can read."""
    with pytest.raises(CallableFlowsError) as error:
        validate_callable_flows(
            db_session,
            callable_flows=[{"flow": "No Such Flow"}],
            account_id=test_user.account_id,
        )
    assert "No Such Flow" in str(error.value)


def test_a_self_reference_needs_the_explicit_flag(db_session, test_user):
    """A flow calling itself is recursion, so it has to be asked for."""
    parent = _flow(db_session, name="Parent Flow", account_id=test_user.account_id)

    with pytest.raises(CallableFlowsError) as error:
        validate_callable_flows(
            db_session,
            callable_flows=[{"flow": "Parent Flow"}],
            account_id=test_user.account_id,
            flow_id=parent.id,
            flow_name=parent.name,
        )
    assert "Parent Flow" in str(error.value)
    assert "allow_self" in str(error.value)

    entries = validate_callable_flows(
        db_session,
        callable_flows=[{"flow": "Parent Flow", "allow_self": True}],
        account_id=test_user.account_id,
        flow_id=parent.id,
        flow_name=parent.name,
    )
    assert entries == [CallableFlowEntry(flow="Parent Flow", allow_self=True)]


def test_a_global_preset_cannot_carry_a_resolvable_allowlist(db_session):
    """With no owning account there is no scope to resolve a reference in."""
    with pytest.raises(CallableFlowsError):
        validate_callable_flows(
            db_session, callable_flows=[{"flow": "Child Flow"}], account_id=None
        )

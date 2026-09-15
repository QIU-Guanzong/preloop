"""What get_execution answers, and what it refuses to answer (#632).

The scope check is the substance of this tool: an execution id is a UUID a
model can invent, so every test that matters here is about a caller reaching
for a row it is not entitled to and being told the same thing every time.
"""

import json
import uuid
from unittest.mock import patch

import pytest

from preloop.a2a.delegation import (
    REFUSAL_REASONS,
    load_schema,
    validate_delegation_task,
)
from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.flow_delegation_call import DelegationUnavailableError
from preloop.services.flow_execution_read import (
    ARTIFACT_BYTES_KEY,
    ARTIFACT_MAX_BYTES_KEY,
    ARTIFACT_POINTER_KEY,
    ARTIFACT_TRUNCATED_KEY,
    FAILURE_CATEGORY_KEY,
    NOT_VISIBLE_REASON,
    execution_record,
    is_visible_to,
    read_execution,
    result_size_cap,
)

AUDIT_TARGET = "preloop.services.dynamic_mcp_server._log_tool_execution_async"


def _flow(db_session, *, name, account_id):
    """A plain flow to hang executions off."""
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=name,
            prompt_template="t",
            agent_type="codex",
            agent_config={},
            allowed_mcp_tools=[{"tool_name": "get_execution"}],
        ),
        account_id=account_id,
    )


def _execution(
    db_session, flow, *, parent=None, depth=0, root=None, status="RUNNING", **columns
):
    """One execution row, optionally as a child of ``parent``."""
    execution = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id,
            status=status,
            parent_execution_id=parent.id if parent is not None else None,
            root_execution_id=root.id if root is not None else None,
            delegation_depth=depth,
        ),
    )
    for column, value in columns.items():
        setattr(execution, column, value)
    db_session.flush()
    db_session.refresh(execution)
    return execution


def _other_account(db_session):
    return crud_account.create(
        db_session,
        obj_in={
            "organization_name": f"other-{uuid.uuid4().hex[:8]}",
            "is_active": True,
        },
    )


@pytest.fixture
def parent_flow(db_session, test_user):
    return _flow(
        db_session,
        name=f"Parent {uuid.uuid4().hex[:6]}",
        account_id=test_user.account_id,
    )


@pytest.fixture
def child_flow(db_session, test_user):
    return _flow(
        db_session,
        name=f"Child {uuid.uuid4().hex[:6]}",
        account_id=test_user.account_id,
    )


@pytest.fixture
def parent(db_session, parent_flow):
    return _execution(db_session, parent_flow)


def _read(db_session, account_id, caller, reference, *, include_result=False):
    """Read one execution with auditing stubbed out."""
    with patch(AUDIT_TARGET):
        return read_execution(
            db_session,
            account_id=str(account_id),
            caller_execution_id=caller.id,
            reference=str(reference),
            include_result=include_result,
        )


def _assert_refused(record):
    """Every refusal is the same refusal, in the same words."""
    validate_delegation_task(record)
    assert record["status"]["state"] == "TASK_STATE_REJECTED"
    assert record["metadata"]["preloop.ai/refusalReason"] == NOT_VISIBLE_REASON
    assert "preloop.ai/executionId" not in record["metadata"]
    assert record.get("artifacts", []) == []


# --- what a caller may read -----------------------------------------------


def test_a_parent_reads_its_own_child(db_session, test_user, parent, child_flow):
    """State, cost and tokens of a child, on the record the parent was handed."""
    child = _execution(
        db_session,
        child_flow,
        parent=parent,
        depth=1,
        root=parent,
        status="SUCCEEDED",
        estimated_cost=0.42,
        total_tokens=1234,
    )

    record = _read(db_session, test_user.account_id, parent, child.id)

    validate_delegation_task(record)
    assert record["id"] == str(child.id)
    assert record["contextId"] == str(parent.id)
    assert record["status"]["state"] == "TASK_STATE_COMPLETED"
    assert record["metadata"]["preloop.ai/executionId"] == str(child.id)
    assert record["metadata"]["preloop.ai/parentExecutionId"] == str(parent.id)
    assert record["metadata"]["preloop.ai/status"] == "SUCCEEDED"
    assert record["metadata"]["preloop.ai/depth"] == 1
    assert record["metadata"]["preloop.ai/cost"] == pytest.approx(0.42)
    assert record["metadata"]["preloop.ai/tokens"] == 1234
    assert record["metadata"]["preloop.ai/flowId"] == str(child_flow.id)


def test_a_terminal_child_returns_its_result_when_it_is_asked_for(
    db_session, test_user, parent, child_flow
):
    """The result is an artifact: the whole document, as stored."""
    payload = {"pr_url": "https://example.com/pr/1", "summary": "done"}
    child = _execution(
        db_session,
        child_flow,
        parent=parent,
        depth=1,
        root=parent,
        status="SUCCEEDED",
        result=payload,
    )

    record = _read(
        db_session, test_user.account_id, parent, child.id, include_result=True
    )

    validate_delegation_task(record)
    artifact = record["artifacts"][0]
    assert artifact["parts"][0]["data"] == payload
    assert artifact["metadata"][ARTIFACT_TRUNCATED_KEY] is False


def test_a_result_is_not_returned_unless_it_is_asked_for(
    db_session, test_user, parent, child_flow
):
    """The flag is the whole difference: same child, no payload."""
    child = _execution(
        db_session,
        child_flow,
        parent=parent,
        depth=1,
        root=parent,
        status="SUCCEEDED",
        result={"summary": "done"},
    )

    record = _read(db_session, test_user.account_id, parent, child.id)

    validate_delegation_task(record)
    assert "artifacts" not in record


def test_a_parent_reads_a_grandchild(db_session, test_user, parent, child_flow):
    """Descendant, not just child: the walk reaches the caller from below."""
    child = _execution(db_session, child_flow, parent=parent, depth=1, root=parent)
    grandchild = _execution(
        db_session, child_flow, parent=child, depth=2, root=parent, status="SUCCEEDED"
    )

    record = _read(db_session, test_user.account_id, parent, grandchild.id)

    validate_delegation_task(record)
    assert record["id"] == str(grandchild.id)
    assert record["metadata"]["preloop.ai/depth"] == 2


def test_an_execution_reads_itself(db_session, test_user, parent):
    """Its own row is in scope: a flow may ask what it has spent."""
    record = _read(db_session, test_user.account_id, parent, parent.id)

    validate_delegation_task(record)
    assert record["id"] == str(parent.id)
    assert record["metadata"]["preloop.ai/depth"] == 0


def test_a_running_child_is_working_and_hands_over_nothing(
    db_session, test_user, parent, child_flow
):
    """No artifact from a child that has not finished, even with the flag."""
    child = _execution(
        db_session,
        child_flow,
        parent=parent,
        depth=1,
        root=parent,
        status="RUNNING",
        result={"partial": True},
    )

    record = _read(
        db_session, test_user.account_id, parent, child.id, include_result=True
    )

    validate_delegation_task(record)
    assert record["status"]["state"] == "TASK_STATE_WORKING"
    assert "artifacts" not in record


def test_a_failed_child_names_its_failure_category(
    db_session, test_user, parent, child_flow
):
    """The category is what a parent branches on; the message is for a human."""
    child = _execution(
        db_session,
        child_flow,
        parent=parent,
        depth=1,
        root=parent,
        status="FAILED",
        failure_category="agent_error",
        error_message="the agent exited non zero",
    )

    record = _read(db_session, test_user.account_id, parent, child.id)

    validate_delegation_task(record)
    assert record["status"]["state"] == "TASK_STATE_FAILED"
    message = record["status"]["message"]
    assert message["metadata"][FAILURE_CATEGORY_KEY] == "agent_error"
    assert message["parts"][0]["text"] == "the agent exited non zero"


# --- what a caller may not read -------------------------------------------


def test_a_sibling_is_refused(db_session, test_user, parent_flow, parent, child_flow):
    """A sibling shares a parent with the caller; it is not below it."""
    root = _execution(db_session, parent_flow)
    caller = _execution(db_session, parent_flow, parent=root, depth=1, root=root)
    sibling = _execution(db_session, child_flow, parent=root, depth=1, root=root)

    _assert_refused(_read(db_session, test_user.account_id, caller, sibling.id))


def test_an_unrelated_execution_of_the_same_account_is_refused(
    db_session, test_user, parent, child_flow
):
    """Same account is not the scope: lineage is."""
    unrelated = _execution(db_session, child_flow)

    _assert_refused(_read(db_session, test_user.account_id, parent, unrelated.id))


def test_an_execution_of_another_account_is_refused(db_session, test_user, parent):
    """A row next door is not readable and is not even acknowledged."""
    other = _other_account(db_session)
    other_flow = _flow(
        db_session, name=f"Other {uuid.uuid4().hex[:6]}", account_id=other.id
    )
    outside = _execution(db_session, other_flow)

    _assert_refused(_read(db_session, test_user.account_id, parent, outside.id))


def test_another_account_and_an_unrelated_execution_are_refused_identically(
    db_session, test_user, parent, child_flow
):
    """The two refusals differ only in the id the caller supplied.

    Anything else would answer "does this execution exist?" for an account
    the caller cannot see.
    """
    other = _other_account(db_session)
    other_flow = _flow(
        db_session, name=f"Other {uuid.uuid4().hex[:6]}", account_id=other.id
    )
    outside = _execution(db_session, other_flow)
    unrelated = _execution(db_session, child_flow)
    absent = uuid.uuid4()

    records = [
        _read(db_session, test_user.account_id, parent, reference)
        for reference in (outside.id, unrelated.id, absent)
    ]

    for record in records:
        _assert_refused(record)
    shapes = set()
    for record, reference in zip(
        records, (outside.id, unrelated.id, absent), strict=False
    ):
        text = json.dumps(record, sort_keys=True)
        text = text.replace(str(reference), "<asked-for>")
        text = text.replace(record["id"], "<attempt>")
        text = text.replace(record["status"]["timestamp"], "<when>")
        shapes.add(text)
    assert len(shapes) == 1


def test_a_reference_that_is_not_an_execution_id_is_refused_the_same_way(
    db_session, test_user, parent
):
    """A non UUID never reaches a query, and is refused in the same words."""
    _assert_refused(_read(db_session, test_user.account_id, parent, "../../etc/passwd"))
    _assert_refused(_read(db_session, test_user.account_id, parent, ""))


def test_a_child_cannot_read_its_parent(db_session, test_user, parent, child_flow):
    """The scope points downwards only: a child is not entitled to its caller."""
    child = _execution(db_session, child_flow, parent=parent, depth=1, root=parent)

    _assert_refused(_read(db_session, test_user.account_id, child, parent.id))


def test_the_refusal_names_the_calling_flow_and_its_depth(
    db_session, test_user, parent, parent_flow
):
    """A refusal record still describes the caller, never the target."""
    record = _read(db_session, test_user.account_id, parent, uuid.uuid4())

    assert record["metadata"]["preloop.ai/flowId"] == str(parent_flow.id)
    assert record["metadata"]["preloop.ai/parentExecutionId"] == str(parent.id)
    assert record["metadata"]["preloop.ai/status"] == "REFUSED"


def test_a_caller_that_is_not_an_execution_gets_no_judgement(db_session, test_user):
    """Nothing to read from means there is no refusal to make either."""

    class _Missing:
        id = uuid.uuid4()

    with patch(AUDIT_TARGET), pytest.raises(DelegationUnavailableError):
        read_execution(
            db_session,
            account_id=str(test_user.account_id),
            caller_execution_id=_Missing.id,
            reference=str(uuid.uuid4()),
        )


def test_visibility_is_decided_on_lineage_not_on_the_root_column(
    db_session, test_user, parent_flow, child_flow
):
    """Two executions of one tree are not visible to each other."""
    root = _execution(db_session, parent_flow)
    left = _execution(db_session, child_flow, parent=root, depth=1, root=root)
    right = _execution(db_session, child_flow, parent=root, depth=1, root=root)

    assert is_visible_to(
        db_session, caller=root, target=left, account_id=test_user.account_id
    )
    assert not is_visible_to(
        db_session, caller=left, target=right, account_id=test_user.account_id
    )


# --- the size cap ----------------------------------------------------------


def test_a_result_above_the_cap_is_truncated_and_says_so(
    db_session, test_user, parent, child_flow
):
    """A fixture larger than the cap comes back cut, flagged and pointed at."""
    cap = result_size_cap()
    payload = {"log": "x" * (cap * 2)}
    child = _execution(
        db_session,
        child_flow,
        parent=parent,
        depth=1,
        root=parent,
        status="SUCCEEDED",
        result=payload,
    )

    record = _read(
        db_session, test_user.account_id, parent, child.id, include_result=True
    )

    validate_delegation_task(record)
    artifact = record["artifacts"][0]
    assert artifact["metadata"][ARTIFACT_TRUNCATED_KEY] is True
    assert artifact["metadata"][ARTIFACT_BYTES_KEY] > cap
    assert artifact["metadata"][ARTIFACT_MAX_BYTES_KEY] == cap
    assert str(child.id) in artifact["metadata"][ARTIFACT_POINTER_KEY]
    assert "Truncated" in artifact["description"]
    body = artifact["parts"][0]["text"]
    assert len(body.encode("utf-8")) <= cap
    assert "data" not in artifact["parts"][0]


def test_the_cap_is_what_bounds_one_read(db_session, test_user, parent, child_flow):
    """The same payload is whole under a larger cap and cut under a smaller."""
    payload = {"summary": "y" * 200}
    child = _execution(
        db_session,
        child_flow,
        parent=parent,
        depth=1,
        root=parent,
        status="SUCCEEDED",
        result=payload,
    )

    whole = execution_record(child, flow=child_flow, include_result=True, cap=4096)
    cut = execution_record(child, flow=child_flow, include_result=True, cap=64)

    validate_delegation_task(whole)
    validate_delegation_task(cut)
    assert whole["artifacts"][0]["parts"][0]["data"] == payload
    assert cut["artifacts"][0]["metadata"][ARTIFACT_TRUNCATED_KEY] is True


def test_the_cap_holds_on_bytes_not_on_characters(
    db_session, test_user, parent, child_flow
):
    """A result in a multibyte script is cut to the cap in bytes."""
    child = _execution(
        db_session,
        child_flow,
        parent=parent,
        depth=1,
        root=parent,
        status="SUCCEEDED",
        result={"summary": "あ" * 200},
    )

    record = execution_record(child, flow=child_flow, include_result=True, cap=100)

    validate_delegation_task(record)
    artifact = record["artifacts"][0]
    assert artifact["metadata"][ARTIFACT_TRUNCATED_KEY] is True
    assert len(artifact["parts"][0]["text"].encode("utf-8")) <= 100


def test_a_result_that_is_not_an_object_comes_back_as_text(
    db_session, test_user, parent, child_flow
):
    """A list is a valid result document and an invalid A2A data part."""
    child = _execution(
        db_session,
        child_flow,
        parent=parent,
        depth=1,
        root=parent,
        status="SUCCEEDED",
        result=["a", "b"],
    )

    record = _read(
        db_session, test_user.account_id, parent, child.id, include_result=True
    )

    validate_delegation_task(record)
    artifact = record["artifacts"][0]
    assert json.loads(artifact["parts"][0]["text"]) == ["a", "b"]
    assert artifact["metadata"][ARTIFACT_TRUNCATED_KEY] is False


# --- auditing --------------------------------------------------------------


def test_one_audit_row_per_permitted_read(db_session, test_user, parent, child_flow):
    """One call, one row, naming the calling execution as the actor."""
    child = _execution(
        db_session, child_flow, parent=parent, depth=1, root=parent, status="SUCCEEDED"
    )

    with patch(AUDIT_TARGET) as audit:
        read_execution(
            db_session,
            account_id=str(test_user.account_id),
            caller_execution_id=parent.id,
            reference=str(child.id),
            correlation_id="corr-1",
            user_id=str(test_user.id),
            api_key_id="key-1",
            api_key_name="flow runtime token",
        )

    audit.assert_called_once()
    kwargs = audit.call_args.kwargs
    assert kwargs["tool_name"] == "get_execution"
    assert kwargs["execution_id"] == str(parent.id)
    assert kwargs["user_id"] == str(test_user.id)
    assert kwargs["api_key_id"] == "key-1"
    assert kwargs["correlation_id"] == "corr-1"
    assert kwargs["status"] == "read:TASK_STATE_COMPLETED"
    assert kwargs["tool_args"]["execution_id"] == str(child.id)


def test_one_audit_row_per_refused_read(db_session, test_user, parent):
    """A refusal is a call too, and is the one worth having a row for."""
    with patch(AUDIT_TARGET) as audit:
        read_execution(
            db_session,
            account_id=str(test_user.account_id),
            caller_execution_id=parent.id,
            reference=str(uuid.uuid4()),
            correlation_id="corr-2",
        )

    audit.assert_called_once()
    assert audit.call_args.kwargs["status"] == f"refused:{NOT_VISIBLE_REASON}"
    assert audit.call_args.kwargs["execution_id"] == str(parent.id)


def test_an_audit_failure_does_not_break_a_read(
    db_session, test_user, parent, child_flow
):
    """The answer is the product; the row is a side effect."""
    child = _execution(
        db_session, child_flow, parent=parent, depth=1, root=parent, status="SUCCEEDED"
    )

    with patch(AUDIT_TARGET, side_effect=RuntimeError("audit down")):
        record = read_execution(
            db_session,
            account_id=str(test_user.account_id),
            caller_execution_id=parent.id,
            reference=str(child.id),
        )

    validate_delegation_task(record)


# --- the frozen vocabulary -------------------------------------------------


def test_the_read_refusal_reason_is_in_the_frozen_vocabulary():
    """The reason a read is refused with is part of #625's list, not prose."""
    assert NOT_VISIBLE_REASON in REFUSAL_REASONS


def test_the_schema_enum_and_the_reason_list_agree():
    """One vocabulary: the schema and the constant cannot drift apart."""
    schema = load_schema("delegation_task")
    enum = schema["$defs"]["taskMetadata"]["properties"]["preloop.ai/refusalReason"][
        "enum"
    ]
    assert list(enum) == list(REFUSAL_REASONS)

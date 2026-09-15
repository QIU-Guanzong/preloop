"""Tests for the execution tree read (#634).

A delegating run is invisible while its work sits in other executions: the
parent's page shows one row and its own cost, and the children are only
findable by id. ``GET /flows/executions/{id}/tree`` answers "what did this
run start, how did it go and what did it cost", with the subtree total kept
separate from the parent's own cost so the two are never conflated.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_flow, crud_flow_execution
from preloop.models.models import Flow
from preloop.models.models.flow_execution import (
    DELEGATION_DETAILS_KEY,
    FlowExecution,
)
from preloop.models.models.user import User
from preloop.models.schemas.flow import FlowCreate

# One fixture, used by every assertion below and mirrored by the console test
# for preloop-execution-tree, so the two halves of the feature agree on what
# the same tree costs.
PARENT_COST = 0.11
CHILD_COSTS = {"lint the diff": 0.05, "run the migrations": 0.02, "draft the note": 0.0}
GRANDCHILD_COST = 0.01
SUBTREE_COST = round(sum(CHILD_COSTS.values()) + GRANDCHILD_COST, 4)


def _flow(db: Session, account_id, name: str) -> Flow:
    """A flow owned by one account, with the fields the model requires."""
    return crud_flow.create(
        db=db,
        flow_in=FlowCreate(
            name=f"{name} {uuid.uuid4().hex[:8]}",
            description="Flow used by the execution tree tests",
            trigger_event_source="manual",
            trigger_event_types=["test"],
            prompt_template="Do the thing",
            agent_type="codex",
            agent_config={},
            account_id=account_id,
        ),
        account_id=account_id,
    )


def _execution(
    db: Session,
    *,
    flow: Flow,
    status: str = "SUCCEEDED",
    parent: FlowExecution = None,
    root_execution_id: uuid.UUID = None,
    depth: int = 0,
    label: str = None,
    cost: float = 0.0,
    tokens: int = 0,
    tool_calls: int = 0,
    failure_category: str = None,
    started_minutes_ago: int = 10,
    ran_seconds: int = 90,
) -> FlowExecution:
    """Create one execution row, optionally as the delegated child of another."""
    start = datetime.now(timezone.utc) - timedelta(minutes=started_minutes_ago)
    details = {"source": "manual"}
    if parent is not None:
        details = {
            "source": "flow_delegation",
            DELEGATION_DETAILS_KEY: {
                "parent_execution_id": str(parent.id),
                "root_execution_id": str(root_execution_id),
                "parent_flow_id": str(parent.flow_id),
                "depth": depth,
                "label": label,
            },
        }
    execution = FlowExecution(
        flow_id=flow.id,
        status=status,
        start_time=start,
        end_time=(start + timedelta(seconds=ran_seconds)) if ran_seconds else None,
        trigger_event_details=details,
        estimated_cost=cost,
        total_tokens=tokens,
        tool_calls_count=tool_calls,
        failure_category=failure_category,
        parent_execution_id=parent.id if parent is not None else None,
        root_execution_id=root_execution_id,
        delegation_depth=depth,
    )
    db.add(execution)
    db.flush()
    db.refresh(execution)
    return execution


@pytest.fixture
def parent_flow(db_session: Session, test_user: User) -> Flow:
    return _flow(db_session, test_user.account_id, "Delegating flow")


@pytest.fixture
def child_flow(db_session: Session, test_user: User) -> Flow:
    return _flow(db_session, test_user.account_id, "Delegated flow")


@pytest.fixture
def tree(db_session: Session, parent_flow: Flow, child_flow: Flow) -> dict:
    """A parent with three children, one of which has a grandchild."""
    parent = _execution(
        db_session,
        flow=parent_flow,
        status="RUNNING",
        cost=PARENT_COST,
        tokens=4000,
        tool_calls=9,
        ran_seconds=0,
    )
    lint = _execution(
        db_session,
        flow=child_flow,
        parent=parent,
        root_execution_id=parent.id,
        depth=1,
        label="lint the diff",
        cost=CHILD_COSTS["lint the diff"],
        tokens=1200,
        tool_calls=7,
        started_minutes_ago=9,
    )
    migrations = _execution(
        db_session,
        flow=child_flow,
        parent=parent,
        root_execution_id=parent.id,
        depth=1,
        status="FAILED",
        label="run the migrations",
        cost=CHILD_COSTS["run the migrations"],
        tokens=300,
        tool_calls=2,
        failure_category="agent_error",
        started_minutes_ago=8,
    )
    note = _execution(
        db_session,
        flow=child_flow,
        parent=parent,
        root_execution_id=parent.id,
        depth=1,
        status="PENDING",
        label="draft the note",
        cost=CHILD_COSTS["draft the note"],
        started_minutes_ago=7,
        ran_seconds=0,
    )
    grandchild = _execution(
        db_session,
        flow=child_flow,
        parent=lint,
        root_execution_id=parent.id,
        depth=2,
        label="fix what the linter found",
        cost=GRANDCHILD_COST,
        tokens=100,
        tool_calls=1,
        started_minutes_ago=6,
    )
    db_session.flush()
    return {
        "parent": parent,
        "lint": lint,
        "migrations": migrations,
        "note": note,
        "grandchild": grandchild,
    }


class TestExecutionTreeEndpoint:
    def test_execution_with_no_children_has_an_empty_tree(
        self, client: TestClient, db_session: Session, parent_flow: Flow
    ):
        """The overwhelming majority of runs: an empty tree, not a 404."""
        execution = _execution(db_session, flow=parent_flow, cost=0.03)

        response = client.get(f"/api/v1/flows/executions/{execution.id}/tree")

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["executions"] == []
        assert data["rollup"]["total"] == 0
        assert data["rollup"]["by_status"] == {}
        assert data["rollup"]["total_estimated_cost"] == 0.0
        assert data["rollup"]["total_tokens"] == 0
        assert data["truncated"] is False
        # Its own cost still comes back, and is not part of any subtree total.
        assert data["execution"]["estimated_cost"] == 0.03
        assert data["root_execution_id"] == str(execution.id)

    def test_a_parent_lists_its_children_with_flow_label_state_times_and_cost(
        self, client: TestClient, tree: dict, child_flow: Flow
    ):
        response = client.get(f"/api/v1/flows/executions/{tree['parent'].id}/tree")

        assert response.status_code == 200, response.text
        rows = response.json()["executions"]
        direct = [r for r in rows if r["parent_execution_id"] == str(tree["parent"].id)]
        assert len(direct) == 3
        for row in direct:
            assert row["flow_name"] == child_flow.name
            assert row["start_time"]
            assert row["estimated_cost"] is not None
            assert row["delegation_depth"] == 1
        assert [row["label"] for row in direct] == [
            "lint the diff",
            "run the migrations",
            "draft the note",
        ]
        assert [row["status"] for row in direct] == ["SUCCEEDED", "FAILED", "PENDING"]
        # Duration is start to end; the console derives it, so both ends ship.
        assert direct[0]["end_time"] is not None
        assert direct[2]["end_time"] is None

    def test_the_grandchild_is_in_the_tree_under_its_own_parent(
        self, client: TestClient, tree: dict
    ):
        """The console expands to it; the read hands over the whole subtree."""
        response = client.get(f"/api/v1/flows/executions/{tree['parent'].id}/tree")

        rows = response.json()["executions"]
        assert len(rows) == 4
        grandchild = next(r for r in rows if r["id"] == str(tree["grandchild"].id))
        assert grandchild["parent_execution_id"] == str(tree["lint"].id)
        assert grandchild["delegation_depth"] == 2
        assert grandchild["label"] == "fix what the linter found"
        # Parents before children, so one pass builds the tree.
        ids = [r["id"] for r in rows]
        assert ids.index(str(tree["lint"].id)) < ids.index(str(tree["grandchild"].id))

    def test_a_child_answers_with_its_own_subtree_only(
        self, client: TestClient, tree: dict
    ):
        response = client.get(f"/api/v1/flows/executions/{tree['lint'].id}/tree")

        assert response.status_code == 200, response.text
        data = response.json()
        assert [r["id"] for r in data["executions"]] == [str(tree["grandchild"].id)]
        assert data["root_execution_id"] == str(tree["parent"].id)
        assert data["execution"]["id"] == str(tree["lint"].id)
        assert data["execution"]["label"] == "lint the diff"
        assert data["rollup"]["total_estimated_cost"] == GRANDCHILD_COST

    def test_the_subtree_total_is_the_sum_of_the_descendants(
        self, client: TestClient, tree: dict
    ):
        data = client.get(f"/api/v1/flows/executions/{tree['parent'].id}/tree").json()

        rollup = data["rollup"]
        assert rollup["total"] == 4
        assert rollup["by_status"] == {
            "SUCCEEDED": 2,
            "FAILED": 1,
            "PENDING": 1,
        }
        assert rollup["completed"] == 3
        assert rollup["total_estimated_cost"] == SUBTREE_COST
        assert rollup["total_estimated_cost"] == round(
            sum(row["estimated_cost"] for row in data["executions"]), 4
        )
        assert rollup["total_tokens"] == 1600
        assert rollup["total_tool_calls"] == 10

    def test_the_parents_own_cost_is_separate_from_the_subtree_total(
        self, client: TestClient, tree: dict
    ):
        data = client.get(f"/api/v1/flows/executions/{tree['parent'].id}/tree").json()

        assert data["execution"]["estimated_cost"] == PARENT_COST
        assert data["execution"]["total_tokens"] == 4000
        assert data["rollup"]["total_estimated_cost"] == SUBTREE_COST
        # The parent is never counted in the rollup over what it started.
        assert data["execution"]["id"] not in [r["id"] for r in data["executions"]]
        assert data["rollup"]["total_estimated_cost"] != round(
            PARENT_COST + SUBTREE_COST, 4
        )

    def test_a_failed_child_carries_its_failure_category(
        self, client: TestClient, tree: dict
    ):
        rows = client.get(f"/api/v1/flows/executions/{tree['parent'].id}/tree").json()[
            "executions"
        ]

        failed = next(r for r in rows if r["status"] == "FAILED")
        assert failed["failure_category"] == "agent_error"
        assert all(
            r["failure_category"] is None for r in rows if r["status"] != "FAILED"
        )

    def test_an_unknown_execution_is_404(self, client: TestClient):
        response = client.get(f"/api/v1/flows/executions/{uuid.uuid4()}/tree")
        assert response.status_code == 404

    def test_another_accounts_execution_is_404(
        self, client: TestClient, db_session: Session
    ):
        other_account = crud_account.create(
            db_session,
            obj_in={"organization_name": f"Other Org {uuid.uuid4().hex[:8]}"},
        )
        other_flow = _flow(db_session, other_account.id, "Foreign flow")
        foreign = _execution(db_session, flow=other_flow)

        response = client.get(f"/api/v1/flows/executions/{foreign.id}/tree")

        assert response.status_code == 404

    def test_a_foreign_child_is_not_listed_under_a_local_root(
        self,
        client: TestClient,
        db_session: Session,
        tree: dict,
    ):
        """Lineage ids are not authorisation: the read joins through the flow."""
        other_account = crud_account.create(
            db_session,
            obj_in={"organization_name": f"Other Org {uuid.uuid4().hex[:8]}"},
        )
        other_flow = _flow(db_session, other_account.id, "Foreign flow")
        _execution(
            db_session,
            flow=other_flow,
            parent=tree["parent"],
            root_execution_id=tree["parent"].id,
            depth=1,
            label="should not be visible",
        )

        rows = client.get(f"/api/v1/flows/executions/{tree['parent'].id}/tree").json()[
            "executions"
        ]

        assert len(rows) == 4
        assert all(r["label"] != "should not be visible" for r in rows)

    def test_a_run_that_was_not_delegated_has_no_label(
        self, client: TestClient, db_session: Session, parent_flow: Flow
    ):
        execution = _execution(db_session, flow=parent_flow)

        data = client.get(f"/api/v1/flows/executions/{execution.id}/tree").json()

        assert data["execution"]["label"] is None

    def test_the_tree_is_capped_and_says_so(
        self,
        client: TestClient,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
        tree: dict,
    ):
        """A configurable fan-out cap is not a bound on the read."""
        from preloop.api.endpoints import flows as flows_endpoints

        monkeypatch.setattr(flows_endpoints, "EXECUTION_TREE_MAX_NODES", 2)

        data = client.get(f"/api/v1/flows/executions/{tree['parent'].id}/tree").json()

        assert data["truncated"] is True
        assert len(data["executions"]) <= 2


class TestLineageRead:
    def test_the_lineage_query_projects_the_label_without_the_payload(
        self, db_session: Session, test_user: User, tree: dict
    ):
        """One string is projected; the trigger payload stays on the server."""
        root_id = tree["parent"].id
        account_id = test_user.account_id
        # The fixture built these rows in this session, so they are already in
        # the identity map; drop them to read what the query alone returns.
        db_session.expunge_all()

        rows = crud_flow_execution.get_lineage(
            db_session,
            root_execution_id=root_id,
            account_id=account_id,
        )

        assert len(rows) == 4
        labels = {row.delegation_label for row in rows}
        assert "lint the diff" in labels
        for row in rows:
            assert "trigger_event_details" in inspect(row).unloaded

    def test_the_root_is_not_returned_by_its_own_lineage(
        self, db_session: Session, test_user: User, tree: dict
    ):
        rows = crud_flow_execution.get_lineage(
            db_session,
            root_execution_id=tree["parent"].id,
            account_id=test_user.account_id,
        )

        assert str(tree["parent"].id) not in {str(row.id) for row in rows}

    def test_rows_come_back_shallowest_first(
        self, db_session: Session, test_user: User, tree: dict
    ):
        rows = crud_flow_execution.get_lineage(
            db_session,
            root_execution_id=tree["parent"].id,
            account_id=test_user.account_id,
        )

        assert [row.delegation_depth for row in rows] == [1, 1, 1, 2]

    def test_the_limit_bounds_the_read(
        self, db_session: Session, test_user: User, tree: dict
    ):
        rows = crud_flow_execution.get_lineage(
            db_session,
            root_execution_id=tree["parent"].id,
            account_id=test_user.account_id,
            limit=2,
        )

        assert len(rows) == 2


def test_the_delegation_details_key_matches_the_writers_spelling():
    """The model restates the key the delegation service writes."""
    from preloop.services import flow_delegation_call

    assert DELEGATION_DETAILS_KEY == flow_delegation_call.DELEGATION_DETAILS_KEY

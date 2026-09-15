"""A label trigger must fire on the label the event carries, once per object.

Replays the 2026-09-15 incident: a flow triggering on ``issue_labeled`` with
``{"labels": ["agent-ready"]}`` started one execution per label on the issue
instead of one per matching label event. Four issues created with six labels
each produced 21 executions and three duplicate pull requests.

Two independent guards are covered here:

* the condition is evaluated against the label the delivery carries, so five
  of the six deliveries no longer match at all;
* at most one execution of a flow is active per tracker object, so any
  future burst (a human applying two configured labels, a redelivery a
  delivery key cannot catch) still costs one run.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from preloop.services.flow_trigger_service import FlowTriggerService

pytestmark = pytest.mark.asyncio

ISSUE_LABELS = (
    "a2a",
    "backend",
    "complexity:low",
    "readiness:ready",
    "risk:low",
    "agent-ready",
)


@pytest.fixture
def mock_db():
    """Mock session whose delivery-idempotency lookups answer "no"."""
    db = MagicMock(spec=Session)
    filtered = db.query.return_value.filter.return_value
    filtered.filter.return_value = filtered
    filtered.order_by.return_value.first.return_value = None
    return db


@pytest.fixture
def service(mock_db):
    return FlowTriggerService(mock_db, MagicMock())


@pytest.fixture
def account_id():
    return str(uuid.uuid4())


@pytest.fixture
def labeled_flow():
    """Implementation flow: fire when ``agent-ready`` is applied."""
    flow = MagicMock()
    flow.id = uuid.uuid4()
    flow.name = "Automated Issue Implementation"
    flow.is_enabled = True
    flow.trigger_config = {"filter_conditions": {"labels": ["agent-ready"]}}
    flow.prompt_template = "implement it"
    flow.allowed_mcp_tools = []
    flow.webhook_config = None
    return flow


def github_labeled_event(
    label: str,
    *,
    account_id: str,
    issue_labels: tuple = ISSUE_LABELS,
    number: int = 626,
    action: str = "labeled",
) -> dict:
    """One GitHub ``issues.labeled`` delivery: a single subject label."""
    return {
        "source": "github",
        "type": "issue_labeled" if action == "labeled" else "issue_unlabeled",
        "account_id": account_id,
        "payload": {
            "action": action,
            "label": {"name": label},
            "issue": {
                "number": number,
                "title": "Add a callable flows picker",
                "labels": [{"name": name} for name in issue_labels],
                "user": {"login": "jane-doe"},
            },
            "repository": {"full_name": "example-org/example-repo"},
            "sender": {"login": "jane-doe"},
        },
    }


def gitlab_labeled_event(
    added: tuple,
    *,
    account_id: str,
    previous: tuple = (),
    iid: int = 23,
) -> dict:
    """A GitLab issue update whose ``changes.labels`` adds ``added``."""
    current = tuple(previous) + tuple(added)
    return {
        "source": "gitlab",
        "type": "issue_labeled",
        "account_id": account_id,
        "payload": {
            "object_kind": "issue",
            "project": {"path_with_namespace": "example-group/example-project"},
            "object_attributes": {
                "iid": iid,
                "action": "update",
                "labels": [{"title": title} for title in current],
            },
            "labels": [{"title": title} for title in current],
            "changes": {
                "labels": {
                    "previous": [{"title": title} for title in previous],
                    "current": [{"title": title} for title in current],
                }
            },
            "user": {"username": "jane-doe"},
        },
    }


class TestLabelConditionUsesTheEventLabel:
    """``_matches_trigger_config`` reads the delivery, not the object."""

    def test_only_the_configured_label_delivery_matches(self, service, labeled_flow):
        """Six deliveries, one configured label: exactly one match."""
        account = str(uuid.uuid4())
        matches = [
            label
            for label in ISSUE_LABELS
            if service._matches_trigger_config(
                labeled_flow, github_labeled_event(label, account_id=account)
            )
        ]

        assert matches == ["agent-ready"]

    def test_unrelated_label_added_later_does_not_match(self, service, labeled_flow):
        """The issue already carries ``agent-ready``; a new label is not it."""
        event = github_labeled_event(
            "needs-design",
            account_id=str(uuid.uuid4()),
            issue_labels=ISSUE_LABELS + ("needs-design",),
        )

        assert service._matches_trigger_config(labeled_flow, event) is False

    def test_unlabeled_delivery_matches_the_removed_label(self, service, labeled_flow):
        """An ``unlabeled`` flow filters on the label that left."""
        labeled_flow.trigger_config = {"filter_conditions": {"labels": ["agent-ready"]}}
        event = github_labeled_event(
            "agent-ready",
            account_id=str(uuid.uuid4()),
            issue_labels=("a2a", "backend"),
            action="unlabeled",
        )

        assert service._matches_trigger_config(labeled_flow, event) is True

    def test_enriched_payload_uses_added_labels(self, service, labeled_flow):
        """The production path merges ``extract_filter_fields`` in first."""
        from preloop.sync.event_normalizer import extract_filter_fields

        event = github_labeled_event("readiness:ready", account_id=str(uuid.uuid4()))
        event["payload"].update(
            extract_filter_fields("github", "issues", event["payload"])
        )

        assert service._matches_trigger_config(labeled_flow, event) is False

    def test_gitlab_label_edit_matches_only_the_added_label(
        self, service, labeled_flow
    ):
        """GitLab folds label edits into an update hook with a delta."""
        account = str(uuid.uuid4())
        unrelated = gitlab_labeled_event(
            ("needs-design",), account_id=account, previous=ISSUE_LABELS
        )
        configured = gitlab_labeled_event(
            ("agent-ready",), account_id=account, previous=ISSUE_LABELS[:-1]
        )

        assert service._matches_trigger_config(labeled_flow, unrelated) is False
        assert service._matches_trigger_config(labeled_flow, configured) is True

    def test_issue_opened_keeps_list_semantics(self, service, labeled_flow):
        """Non-label event types still read the object's label list."""
        from preloop.sync.webhook_payloads import GITHUB_ISSUE_OPENED

        labeled_flow.trigger_config = {"filter_conditions": {"labels": ["bug"]}}
        event = {
            "source": "github",
            "type": "issue_opened",
            "account_id": str(uuid.uuid4()),
            "payload": GITHUB_ISSUE_OPENED,
        }

        assert service._matches_trigger_config(labeled_flow, event) is True

    def test_label_event_without_a_label_falls_back_to_the_list(
        self, service, labeled_flow
    ):
        """An unknown payload shape keeps working rather than going silent."""
        event = {
            "source": "github",
            "type": "issue_labeled",
            "account_id": str(uuid.uuid4()),
            "payload": {
                "issue": {
                    "number": 626,
                    "labels": [{"name": name} for name in ISSUE_LABELS],
                },
                "repository": {"full_name": "example-org/example-repo"},
            },
        }

        assert service._matches_trigger_config(labeled_flow, event) is True

    def test_bound_implementation_comment_still_bypasses_labels(
        self, service, labeled_flow
    ):
        """Regression: a PR bound to a qualified issue needs no label."""
        event = {
            "source": "github",
            "type": "comment_created",
            "account_id": str(uuid.uuid4()),
            "payload": {
                "action": "created",
                "comment": {"body": "please rebase"},
                "issue": {
                    "number": 700,
                    "pull_request": {"url": "https://example.com/pr/700"},
                    "labels": [],
                },
                "repository": {"full_name": "example-org/example-repo"},
            },
        }

        with patch(
            "preloop.services.flow_pr_binding.is_bound_implementation_comment",
            return_value=True,
        ):
            assert service._matches_trigger_config(labeled_flow, event) is True


class TestOneActiveExecutionPerTrackerObject:
    """``process_event`` creates at most one active run per (flow, object)."""

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    @patch.object(FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock)
    async def test_six_label_deliveries_start_one_execution(
        self,
        mock_start,
        mock_crud_flow,
        mock_nats,
        mock_crud_execution,
        service,
        labeled_flow,
        account_id,
    ):
        """The incident: one issue, six labels, one execution."""
        mock_nats.return_value = AsyncMock()
        mock_crud_flow.get_by_trigger.return_value = [labeled_flow]
        mock_crud_execution.get_running_by_flow.return_value = []

        for label in ISSUE_LABELS:
            await service.process_event(
                github_labeled_event(label, account_id=account_id)
            )

        assert mock_start.await_count == 1

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    @patch.object(FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock)
    async def test_unrelated_label_added_later_starts_nothing(
        self,
        mock_start,
        mock_crud_flow,
        mock_nats,
        mock_crud_execution,
        service,
        labeled_flow,
        account_id,
    ):
        """A label the flow does not ask for never starts a run."""
        mock_nats.return_value = AsyncMock()
        mock_crud_flow.get_by_trigger.return_value = [labeled_flow]
        mock_crud_execution.get_running_by_flow.return_value = []

        await service.process_event(
            github_labeled_event(
                "needs-design",
                account_id=account_id,
                issue_labels=ISSUE_LABELS + ("needs-design",),
            )
        )

        mock_start.assert_not_awaited()

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    @patch.object(FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock)
    async def test_configured_label_reapplied_while_active_starts_nothing(
        self,
        mock_start,
        mock_crud_flow,
        mock_nats,
        mock_crud_execution,
        service,
        labeled_flow,
        account_id,
    ):
        """An active run on the same issue coalesces the second trigger."""
        mock_nats.return_value = AsyncMock()
        mock_crud_flow.get_by_trigger.return_value = [labeled_flow]
        event = github_labeled_event("agent-ready", account_id=account_id)

        active = MagicMock()
        active.id = uuid.uuid4()
        active.status = "RUNNING"
        active.trigger_event_details = {
            "source": "github",
            "payload": event["payload"],
        }
        mock_crud_execution.get_running_by_flow.return_value = [active]

        await service.process_event(event)

        mock_start.assert_not_awaited()

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    @patch.object(FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock)
    async def test_parked_execution_also_holds_the_object(
        self,
        mock_start,
        mock_crud_flow,
        mock_nats,
        mock_crud_execution,
        service,
        labeled_flow,
        account_id,
    ):
        """A run waiting for a human owns the issue until it is answered."""
        mock_nats.return_value = AsyncMock()
        mock_crud_flow.get_by_trigger.return_value = [labeled_flow]
        event = github_labeled_event("agent-ready", account_id=account_id)

        parked = MagicMock()
        parked.id = uuid.uuid4()
        parked.status = "WAITING_FOR_HUMAN"
        parked.trigger_event_details = {
            "source": "github",
            "payload": event["payload"],
        }
        mock_crud_execution.get_running_by_flow.return_value = [parked]

        await service.process_event(event)

        mock_start.assert_not_awaited()
        assert (
            "WAITING_FOR_HUMAN"
            in mock_crud_execution.get_running_by_flow.call_args.kwargs[
                "running_statuses"
            ]
        )

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    @patch.object(FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock)
    async def test_label_fires_again_once_the_previous_run_is_terminal(
        self,
        mock_start,
        mock_crud_flow,
        mock_nats,
        mock_crud_execution,
        service,
        labeled_flow,
        account_id,
    ):
        """Nothing active means the object is free again."""
        mock_nats.return_value = AsyncMock()
        mock_crud_flow.get_by_trigger.return_value = [labeled_flow]
        event = github_labeled_event("agent-ready", account_id=account_id)

        finished = MagicMock()
        finished.id = uuid.uuid4()
        finished.status = "SUCCEEDED"
        finished.trigger_event_details = {
            "source": "github",
            "payload": event["payload"],
        }
        # get_running_by_flow filters on status, so a terminal run is absent.
        mock_crud_execution.get_running_by_flow.return_value = []

        await service.process_event(event)

        mock_start.assert_awaited_once()

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    @patch.object(FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock)
    async def test_a_different_issue_is_not_coalesced(
        self,
        mock_start,
        mock_crud_flow,
        mock_nats,
        mock_crud_execution,
        service,
        labeled_flow,
        account_id,
    ):
        """The guard is per object, not per flow."""
        mock_nats.return_value = AsyncMock()
        mock_crud_flow.get_by_trigger.return_value = [labeled_flow]

        active = MagicMock()
        active.id = uuid.uuid4()
        active.status = "RUNNING"
        active.trigger_event_details = {
            "source": "github",
            "payload": github_labeled_event(
                "agent-ready", account_id=account_id, number=626
            )["payload"],
        }
        mock_crud_execution.get_running_by_flow.return_value = [active]

        await service.process_event(
            github_labeled_event("agent-ready", account_id=account_id, number=999)
        )

        mock_start.assert_awaited_once()

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    @patch.object(FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock)
    async def test_gitlab_label_edits_start_one_execution(
        self,
        mock_start,
        mock_crud_flow,
        mock_nats,
        mock_crud_execution,
        service,
        labeled_flow,
        account_id,
    ):
        """GitLab equivalent of the incident, one delta at a time."""
        mock_nats.return_value = AsyncMock()
        mock_crud_flow.get_by_trigger.return_value = [labeled_flow]
        mock_crud_execution.get_running_by_flow.return_value = []

        await service.process_event(
            gitlab_labeled_event(("backend",), account_id=account_id)
        )
        await service.process_event(
            gitlab_labeled_event(
                ("agent-ready",), account_id=account_id, previous=("backend",)
            )
        )
        await service.process_event(
            gitlab_labeled_event(
                ("risk:low",),
                account_id=account_id,
                previous=("backend", "agent-ready"),
            )
        )

        assert mock_start.await_count == 1

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    @patch.object(FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock)
    async def test_pr_comment_is_not_coalesced_away(
        self,
        mock_start,
        mock_crud_flow,
        mock_nats,
        mock_crud_execution,
        service,
        labeled_flow,
        account_id,
    ):
        """Regression: comments must still reach the PR-bound resume path.

        A comment on a PR that has a live execution is exactly the case the
        coalescing guard would otherwise swallow, and it is how a reviewer
        feeds an in-flight run.
        """
        labeled_flow.trigger_config = None
        mock_nats.return_value = AsyncMock()
        mock_crud_flow.get_by_trigger.return_value = [labeled_flow]

        event = {
            "source": "github",
            "type": "comment_created",
            "account_id": account_id,
            "payload": {
                "action": "created",
                "comment": {"body": "please rebase", "user": {"login": "jane-doe"}},
                "issue": {
                    "number": 700,
                    "labels": [],
                    "pull_request": {"url": "https://example.com/pr/700"},
                },
                "repository": {"full_name": "example-org/example-repo"},
                "sender": {"login": "jane-doe"},
            },
        }
        active = MagicMock()
        active.id = uuid.uuid4()
        active.status = "RUNNING"
        active.trigger_event_details = {"source": "github", "payload": event["payload"]}
        mock_crud_execution.get_running_by_flow.return_value = [active]

        with patch(
            "preloop.services.flow_pr_binding.flow_requires_pr_comment_resume",
            return_value=False,
        ):
            await service.process_event(event)

        mock_start.assert_awaited_once()

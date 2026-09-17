"""Automatic triage runs only when the delivery changed what triage reads.

GitLab has no assignment hook: assigning an issue, moving its milestone or
setting a due date arrives as a plain issue update, which used to start a full
triage run that reassessed an unchanged title and description. GitHub sends
``edited`` only for title/body, so the guard is a no-op there.

The gate is relevance filtering at the trigger boundary. It is not durable
per-revision coalescing: two deliveries that both edit the description still
start two runs once the first finishes (issue #448).
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.services.issue_triage_trigger import (
    is_triage_preset_flow,
    issue_update_touches_content,
    skip_triage_flow_for_event,
)

pytestmark = pytest.mark.asyncio

TRIAGE_PRESET_NAME = "Issue Triage Assistant"


@pytest.fixture
def mock_db():
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
def preset_id():
    return uuid.uuid4()


def _flow(*, name: str, source_preset_id=None) -> MagicMock:
    flow = MagicMock()
    flow.id = uuid.uuid4()
    flow.name = name
    flow.is_enabled = True
    flow.is_preset = False
    flow.source_preset_id = source_preset_id
    flow.trigger_config = None
    flow.prompt_template = "assess it"
    flow.allowed_mcp_tools = []
    flow.webhook_config = None
    return flow


def gitlab_update_event(changes: dict, *, account_id: str, iid: int = 23) -> dict:
    """A GitLab ``Issue Hook`` update carrying a provider change set."""
    return {
        "source": "gitlab",
        "type": "issue_updated",
        "account_id": account_id,
        "payload": {
            "object_kind": "issue",
            "project": {"path_with_namespace": "example-group/example-project"},
            "object_attributes": {
                "iid": iid,
                "action": "update",
                "title": "Search returns 500",
                "description": "Reproduced on the search endpoint",
                "labels": [],
            },
            "changes": changes,
            "user": {"username": "jane-doe"},
        },
    }


class TestDeliveryRelevance:
    """``issue_update_touches_content`` reads the provider change set only."""

    @pytest.mark.parametrize(
        "changes",
        [
            {"title": {"previous": "a", "current": "b"}},
            {"description": {"previous": "a", "current": "b"}},
            {"body": {"from": "a"}},
            {"assignees": {"previous": [], "current": [1]}, "description": {}},
        ],
    )
    async def test_content_changes_are_relevant(self, changes, account_id):
        event = gitlab_update_event(changes, account_id=account_id)
        assert issue_update_touches_content(event) is True

    @pytest.mark.parametrize(
        "changes",
        [
            {"assignees": {"previous": [], "current": [{"id": 1}]}},
            {"milestone_id": {"previous": None, "current": 4}},
            {"due_date": {"previous": None, "current": "2026-10-01"}},
            {"updated_at": {"previous": "x", "current": "y"}},
        ],
    )
    async def test_metadata_only_changes_are_not_relevant(self, changes, account_id):
        event = gitlab_update_event(changes, account_id=account_id)
        assert issue_update_touches_content(event) is False

    async def test_other_event_types_are_always_relevant(self, account_id):
        opened = gitlab_update_event({"assignees": {}}, account_id=account_id)
        opened["type"] = "issue_opened"
        assert issue_update_touches_content(opened) is True

    @pytest.mark.parametrize("changes", [None, {}, "not-a-mapping", []])
    async def test_without_a_change_set_the_delivery_is_kept(self, changes, account_id):
        """No evidence of irrelevance is not evidence of irrelevance."""
        event = gitlab_update_event({}, account_id=account_id)
        if changes is None:
            event["payload"].pop("changes")
        else:
            event["payload"]["changes"] = changes
        assert issue_update_touches_content(event) is True

    async def test_payload_that_is_not_a_mapping_is_kept(self, account_id):
        assert (
            issue_update_touches_content(
                {"type": "issue_updated", "payload": "opaque", "account_id": account_id}
            )
            is True
        )


class TestTriageFlowIdentity:
    """Only an account copy of the triage preset is held back."""

    async def test_source_preset_identifies_the_flow(self, mock_db, preset_id):
        flow = _flow(name="Renamed triage", source_preset_id=preset_id)
        preset = MagicMock()
        preset.id = preset_id
        with (
            patch(
                "preloop.flow_presets.PRESET_SLUGS",
                {"issue-triage-assistant": TRIAGE_PRESET_NAME},
            ),
            patch(
                "preloop.models.crud.crud_flow.get_global_preset_by_name",
                return_value=preset,
            ),
        ):
            assert is_triage_preset_flow(mock_db, flow) is True

    async def test_another_preset_copy_is_not_a_triage_flow(self, mock_db, preset_id):
        flow = _flow(
            name="Automated Issue Implementation", source_preset_id=uuid.uuid4()
        )
        preset = MagicMock()
        preset.id = preset_id
        with (
            patch(
                "preloop.flow_presets.PRESET_SLUGS",
                {"issue-triage-assistant": TRIAGE_PRESET_NAME},
            ),
            patch(
                "preloop.models.crud.crud_flow.get_global_preset_by_name",
                return_value=preset,
            ),
        ):
            assert is_triage_preset_flow(mock_db, flow) is False

    async def test_name_fallback_without_a_source_preset(self, mock_db):
        """Mirrors resolve_or_create_flow's own fallback for older flows."""
        with patch(
            "preloop.flow_presets.PRESET_SLUGS",
            {"issue-triage-assistant": TRIAGE_PRESET_NAME},
        ):
            assert (
                is_triage_preset_flow(mock_db, _flow(name=TRIAGE_PRESET_NAME)) is True
            )
            assert is_triage_preset_flow(mock_db, _flow(name="Something else")) is False

    async def test_a_global_preset_row_is_never_an_account_flow(self, mock_db):
        preset_row = _flow(name=TRIAGE_PRESET_NAME)
        preset_row.is_preset = True
        with patch(
            "preloop.flow_presets.PRESET_SLUGS",
            {"issue-triage-assistant": TRIAGE_PRESET_NAME},
        ):
            assert is_triage_preset_flow(mock_db, preset_row) is False

    async def test_skip_requires_both_an_irrelevant_event_and_a_triage_flow(
        self, mock_db, account_id
    ):
        triage = _flow(name=TRIAGE_PRESET_NAME)
        other = _flow(name="Automated Issue Implementation")
        metadata = gitlab_update_event(
            {"assignees": {"previous": [], "current": [{"id": 1}]}},
            account_id=account_id,
        )
        content = gitlab_update_event(
            {"description": {"previous": "a", "current": "b"}}, account_id=account_id
        )
        with patch(
            "preloop.flow_presets.PRESET_SLUGS",
            {"issue-triage-assistant": TRIAGE_PRESET_NAME},
        ):
            assert skip_triage_flow_for_event(mock_db, triage, metadata) is True
            assert skip_triage_flow_for_event(mock_db, triage, content) is False
            assert skip_triage_flow_for_event(mock_db, other, metadata) is False

    async def test_a_precomputed_relevance_decision_gives_the_same_answer(
        self, mock_db, account_id
    ):
        """A caller filtering many flows evaluates the delivery once."""
        triage = _flow(name=TRIAGE_PRESET_NAME)
        metadata = gitlab_update_event(
            {"assignees": {"previous": [], "current": [{"id": 1}]}},
            account_id=account_id,
        )
        with patch(
            "preloop.flow_presets.PRESET_SLUGS",
            {"issue-triage-assistant": TRIAGE_PRESET_NAME},
        ):
            assert (
                skip_triage_flow_for_event(
                    mock_db, triage, metadata, event_touches_content=False
                )
                is True
            )
            # A caller claiming the delivery is relevant keeps the flow, so the
            # hoisted decision, not a second read of the payload, decides.
            assert (
                skip_triage_flow_for_event(
                    mock_db, triage, metadata, event_touches_content=True
                )
                is False
            )

    async def test_a_custom_flow_named_like_the_preset_inherits_the_gate(
        self, mock_db, account_id
    ):
        """Documented tradeoff of the name fallback, pinned so it stays visible."""
        look_alike = _flow(name=TRIAGE_PRESET_NAME)
        metadata = gitlab_update_event(
            {"assignees": {"previous": [], "current": [{"id": 1}]}},
            account_id=account_id,
        )
        with patch(
            "preloop.flow_presets.PRESET_SLUGS",
            {"issue-triage-assistant": TRIAGE_PRESET_NAME},
        ):
            assert skip_triage_flow_for_event(mock_db, look_alike, metadata) is True
            look_alike.name = "Assignment triage"
            assert skip_triage_flow_for_event(mock_db, look_alike, metadata) is False


class TestProcessEventSkipsIrrelevantTriage:
    """The gate is applied where flows are filtered, before any execution."""

    @staticmethod
    def _patches():
        return (
            patch("preloop.services.flow_trigger_service.crud_flow_execution"),
            patch("preloop.services.flow_trigger_service.get_nats_client"),
            patch("preloop.services.flow_trigger_service.crud_flow"),
            patch.object(
                FlowTriggerService, "_start_flow_execution", new_callable=AsyncMock
            ),
            patch(
                "preloop.flow_presets.PRESET_SLUGS",
                {"issue-triage-assistant": TRIAGE_PRESET_NAME},
            ),
        )

    async def _run(self, service, flow, event):
        (
            crud_execution,
            nats,
            crud_flow_patch,
            start,
            slugs,
        ) = self._patches()
        with (
            crud_execution as execution,
            nats as nats_client,
            crud_flow_patch as flows,
            start as started,
            slugs,
        ):
            nats_client.return_value = AsyncMock()
            flows.get_by_trigger.return_value = [flow]
            execution.get_running_by_flow.return_value = []
            await service.process_event(event)
            return started

    async def test_assignee_only_update_starts_no_triage_run(self, service, account_id):
        started = await self._run(
            service,
            _flow(name=TRIAGE_PRESET_NAME),
            gitlab_update_event(
                {"assignees": {"previous": [], "current": [{"id": 1}]}},
                account_id=account_id,
            ),
        )
        started.assert_not_awaited()

    async def test_description_edit_still_starts_a_triage_run(
        self, service, account_id
    ):
        started = await self._run(
            service,
            _flow(name=TRIAGE_PRESET_NAME),
            gitlab_update_event(
                {"description": {"previous": "a", "current": "b"}},
                account_id=account_id,
            ),
        )
        started.assert_awaited_once()

    async def test_a_non_triage_flow_keeps_the_assignment_delivery(
        self, service, account_id
    ):
        """GitLab assignment normalizes to no other event type; do not drop it."""
        started = await self._run(
            service,
            _flow(name="Automated Issue Implementation"),
            gitlab_update_event(
                {"assignees": {"previous": [], "current": [{"id": 1}]}},
                account_id=account_id,
            ),
        )
        started.assert_awaited_once()

    async def test_the_shipped_gate_is_this_helper(self, service, account_id):
        """process_event must route through the helper, not inline its rule.

        Without this, an edit to the inlined condition would change the shipped
        behavior while the helper's own tests kept passing.
        """
        with patch(
            "preloop.services.flow_trigger_service.skip_triage_flow_for_event",
            return_value=True,
        ) as gate:
            started = await self._run(
                service,
                _flow(name="Automated Issue Implementation"),
                gitlab_update_event(
                    {"description": {"previous": "a", "current": "b"}},
                    account_id=account_id,
                ),
            )
        gate.assert_called_once()
        started.assert_not_awaited()

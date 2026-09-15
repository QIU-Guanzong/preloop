"""Tests for writing gateway search documents off the response path."""

import gc
import weakref
from unittest.mock import patch

import pytest

from preloop.config import settings
from preloop.models.crud import crud_api_usage, crud_gateway_usage_search_document
from preloop.services.gateway_usage_index_queue import (
    GatewayUsageIndexQueue,
    get_gateway_usage_index_queue,
    reset_gateway_usage_index_queue,
)
from preloop.services.gateway_usage_search import (
    GatewayUsageIndexDocument,
    GatewayUsageSearchService,
)


class TrackedDict(dict):
    """A dict that can be weak-referenced, so retention is observable."""


class _KeepOpen:
    """Hand the worker a session it may not close."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self) -> None:
        return None


def _usage(db_session, test_user, *, status_code: int = 200):
    return crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=status_code,
        duration=0.1,
        user_id=str(test_user.id),
        account_id=str(test_user.account_id),
        model_alias="openai/gpt-5",
        provider_name="openai",
        meta_data={"requested_model": "openai/gpt-5", "endpoint_kind": "responses"},
    )


def test_build_index_document_keeps_no_reference_to_the_payloads(db_session, test_user):
    """Nothing built for the corpus may outlive the payloads it read."""
    usage = _usage(db_session, test_user)
    request_payload = TrackedDict(
        {"input": "prompt text " * 200, "api_key": "sk-secret"}
    )
    response_payload = TrackedDict({"output_text": "answer text " * 200})
    request_ref = weakref.ref(request_payload)
    response_ref = weakref.ref(response_payload)

    with (
        patch.object(settings, "model_gateway_auto_index_interactions", True),
        patch.object(settings, "model_gateway_capture_content", True),
    ):
        document = GatewayUsageSearchService().build_index_document(
            usage=usage,
            request_payload=request_payload,
            response_payload=response_payload,
        )

    assert document is not None
    assert "sk-secret" not in document.searchable_text
    queue = GatewayUsageIndexQueue(max_pending=4)
    assert queue.submit(document) is True

    del request_payload
    del response_payload
    gc.collect()

    assert request_ref() is None
    assert response_ref() is None
    assert queue.pending == 1


def test_queued_document_is_written_with_its_own_session(db_session, test_user):
    """The queued write persists the corpus row without the request session."""
    usage = _usage(db_session, test_user)
    with (
        patch.object(settings, "model_gateway_auto_index_interactions", True),
        patch.object(settings, "model_gateway_capture_content", True),
    ):
        document = GatewayUsageSearchService().build_index_document(
            usage=usage,
            request_payload={"input": "queued prompt"},
            response_payload={"output_text": "queued answer"},
        )
    assert document is not None

    queue = GatewayUsageIndexQueue(max_pending=4)
    queue.submit(document)

    def _worker_session():
        # The worker closes the session it was handed; the test session has
        # to outlive it so the assertions below can still read the row.
        yield _KeepOpen(db_session)

    with patch(
        "preloop.services.gateway_usage_index_queue.get_db_session",
        side_effect=lambda: _worker_session(),
    ):
        assert queue.drain() == 1

    stored = crud_gateway_usage_search_document.get_by_api_usage_id(
        db_session, api_usage_id=str(usage.id)
    )
    assert stored is not None
    assert "queued prompt" in stored.searchable_text
    assert "queued answer" in stored.searchable_text
    assert queue.written == 1
    assert queue.pending == 0


def test_queue_drops_documents_instead_of_growing_without_bound():
    """A backed-up queue drops work rather than adding to memory pressure."""
    queue = GatewayUsageIndexQueue(max_pending=1)
    first = GatewayUsageIndexDocument(
        api_usage_id="usage-1", searchable_text="first", meta_data={}
    )
    second = GatewayUsageIndexDocument(
        api_usage_id="usage-2", searchable_text="second", meta_data={}
    )

    assert queue.submit(first) is True
    assert queue.submit(second) is False
    assert queue.dropped == 1
    assert queue.pending == 1


def test_write_failure_is_isolated_from_the_caller():
    """A failed index write is logged, never raised at the gateway."""
    queue = GatewayUsageIndexQueue(max_pending=2)
    queue.submit(
        GatewayUsageIndexDocument(
            api_usage_id="usage-1", searchable_text="text", meta_data={}
        )
    )

    class _Session:
        def rollback(self) -> None:
            return None

        def close(self) -> None:
            return None

    def _worker_session():
        yield _Session()

    with (
        patch(
            "preloop.services.gateway_usage_index_queue.get_db_session",
            side_effect=lambda: _worker_session(),
        ),
        patch(
            "preloop.services.gateway_usage_index_queue."
            "crud_gateway_usage_search_document.upsert_for_api_usage_id",
            side_effect=RuntimeError("index write failed"),
        ),
    ):
        assert queue.drain() == 0

    assert queue.failed == 1
    assert queue.pending == 0


def test_reset_gateway_usage_index_queue_drops_the_singleton():
    """Tests can drop the process-wide queue so later cases see a fresh one."""
    first = get_gateway_usage_index_queue()
    assert get_gateway_usage_index_queue() is first
    reset_gateway_usage_index_queue()
    assert get_gateway_usage_index_queue() is not first


def test_process_queue_max_pending_follows_settings():
    """The singleton queue depth is the settings knob, not a hardcoded size."""
    with patch.object(settings, "gateway_usage_index_queue_max_pending", 1):
        reset_gateway_usage_index_queue()
        queue = get_gateway_usage_index_queue()
        first = GatewayUsageIndexDocument(
            api_usage_id="usage-1", searchable_text="first", meta_data={}
        )
        second = GatewayUsageIndexDocument(
            api_usage_id="usage-2", searchable_text="second", meta_data={}
        )
        assert queue.submit(first) is True
        assert queue.submit(second) is False
        assert queue.dropped == 1


@pytest.mark.parametrize("status_code", [200, 500])
def test_build_index_document_respects_the_indexing_policy(
    db_session, test_user, status_code
):
    """Policy decisions still happen before any text work."""
    usage = _usage(db_session, test_user, status_code=status_code)
    with patch.object(settings, "model_gateway_auto_index_interactions", False):
        assert (
            GatewayUsageSearchService().build_index_document(
                usage=usage,
                request_payload={"input": "prompt"},
                response_payload={"output_text": "answer"},
            )
            is None
        )

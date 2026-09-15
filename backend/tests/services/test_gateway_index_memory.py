"""Memory cost of indexing concurrent large gateway responses.

Issue #670: gateway replicas were OOMKilled while three agents were calling
``/openai/v1/responses`` at once. Indexing ran on the response path and
copied both payloads before flattening them, so the peak cost scaled with
payload size times concurrency. These tests pin the shape that replaced it:
what indexing adds is bounded by the document size, not by the payloads.
"""

import threading
import tracemalloc
from unittest.mock import patch

from preloop.config import settings
from preloop.models.models.api_usage import ApiUsage
from preloop.services.gateway_usage_search import GatewayUsageSearchService

# One "large response" here is ~1 MB of text, in the range of a long agent
# turn with file context. Three at once is the concurrency that OOMKilled a
# 1Gi replica.
LARGE_PAYLOAD_CHARS = 1_000_000
CONCURRENT_RESPONSES = 3
# A copy of either payload would be ~1 MB. CI (Python 3.11) traced 45893
# bytes for one document build; a 3.14 laptop process traced about 23 KB.
# 2 * MAX_TEXT_CHARS (32 KB) is too tight for 3.11. 256 KB still fails a
# payload copy on both.
MAX_INDEXING_PEAK_BYTES = 256 * 1024


def _usage_row() -> ApiUsage:
    return ApiUsage(
        id="00000000-0000-0000-0000-000000000670",
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=1.0,
        provider_name="openai",
        model_alias="openai/gpt-5",
        meta_data={"endpoint_kind": "responses"},
    )


def _large_payloads() -> tuple[dict, dict]:
    filler = "token " * (LARGE_PAYLOAD_CHARS // 6)
    return (
        {"input": filler, "instructions": "summarize the attached context"},
        {"output_text": filler, "output": [{"type": "message", "content": filler}]},
    )


def test_indexing_allocation_is_bounded_by_the_document_not_the_payload():
    """Building a document must not allocate another copy of the payloads."""
    usage = _usage_row()
    request_payload, response_payload = _large_payloads()
    service = GatewayUsageSearchService()

    with (
        patch.object(settings, "model_gateway_auto_index_interactions", True),
        patch.object(settings, "model_gateway_capture_content", True),
    ):
        tracemalloc.start()
        try:
            document = service.build_index_document(
                usage=usage,
                request_payload=request_payload,
                response_payload=response_payload,
            )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    assert document is not None
    assert peak < MAX_INDEXING_PEAK_BYTES
    assert len(document.searchable_text) <= (
        GatewayUsageSearchService.MAX_TEXT_CHARS + len("\ntruncated: true")
    )


def test_three_concurrent_large_responses_stay_within_one_document_each():
    """The target concurrency from the incident, measured in one process."""
    service = GatewayUsageSearchService()
    payloads = [_large_payloads() for _ in range(CONCURRENT_RESPONSES)]
    documents: list[object] = []
    documents_lock = threading.Lock()
    started = threading.Barrier(CONCURRENT_RESPONSES)

    def _index(index: int) -> None:
        request_payload, response_payload = payloads[index]
        started.wait(timeout=10)
        document = service.build_index_document(
            usage=_usage_row(),
            request_payload=request_payload,
            response_payload=response_payload,
        )
        with documents_lock:
            documents.append(document)

    with (
        patch.object(settings, "model_gateway_auto_index_interactions", True),
        patch.object(settings, "model_gateway_capture_content", True),
    ):
        tracemalloc.start()
        try:
            threads = [
                threading.Thread(target=_index, args=(index,))
                for index in range(CONCURRENT_RESPONSES)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    assert len(documents) == CONCURRENT_RESPONSES
    assert all(document is not None for document in documents)
    # Before this change the same three calls allocated a sanitized copy of
    # every payload, roughly 6 MB on top of the payloads themselves.
    assert peak < CONCURRENT_RESPONSES * MAX_INDEXING_PEAK_BYTES

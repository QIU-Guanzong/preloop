"""Optional hosted spending guard for provider paths without settlement hooks."""

from typing import Any

from preloop.plugins import get_plugin_manager


def guard_unmetered_hosted_call(model: Any) -> None:
    """BYOK/OSS pass; metered hosted adapters must reserve before dispatch."""
    meter = get_plugin_manager().get_service("hosted_spend")
    if meter is not None:
        meter.guard_unmetered(model)

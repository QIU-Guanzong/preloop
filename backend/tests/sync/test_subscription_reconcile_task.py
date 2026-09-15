"""The OSS shim for periodic Stripe subscription reconciliation.

The sync role publishes ``reconcile_stripe_subscriptions`` on an interval.
The implementation is Enterprise; this shim is what the worker actually calls,
so it has to resolve the plugin service, no-op cleanly on an install that has
no billing plugin, and never let a failure escape into the worker loop.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pytest_mock import MockerFixture

from preloop.config import settings
from preloop.sync import tasks


@pytest.fixture
def mock_db(mocker: MockerFixture) -> MagicMock:
    db = MagicMock()
    mocker.patch.object(tasks, "get_db_session", return_value=iter([db]))
    return db


def _plugin_manager(mocker: MockerFixture, service):
    manager = SimpleNamespace(get_service=lambda name: service)
    mocker.patch("preloop.plugins.base.get_plugin_manager", return_value=manager)
    return manager


def test_oss_install_without_the_billing_plugin_no_ops(mocker: MockerFixture):
    """The task must be harmless on an install that never had billing."""
    _plugin_manager(mocker, None)
    get_db = mocker.patch.object(tasks, "get_db_session")

    assert tasks.reconcile_stripe_subscriptions() is None

    get_db.assert_not_called()


def test_the_plugin_service_receives_the_session_and_account(
    mocker: MockerFixture, mock_db: MagicMock
):
    service = MagicMock(return_value={"reconciled": 3})
    _plugin_manager(mocker, service)

    result = tasks.reconcile_stripe_subscriptions(account_id="acc-1")

    service.assert_called_once_with(mock_db, account_id="acc-1")
    assert result == {"reconciled": 3}
    mock_db.close.assert_called_once()


def test_a_failure_is_logged_and_swallowed(mocker: MockerFixture, mock_db: MagicMock):
    """A periodic task that raises takes its worker down with it."""
    service = MagicMock(side_effect=RuntimeError("provider unreachable"))
    _plugin_manager(mocker, service)

    assert tasks.reconcile_stripe_subscriptions() is None

    mock_db.close.assert_called_once()


def test_the_task_is_dispatchable():
    """A task missing from the registry is never delivered to a worker pool."""
    assert "reconcile_stripe_subscriptions" in tasks.DISPATCHABLE_TASKS


def test_the_default_interval_is_six_hours():
    assert settings.billing_subscription_reconcile_hours == 6

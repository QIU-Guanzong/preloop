"""Cloud report windows and physical retention obey different policies."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from preloop.models import models
from preloop.models.crud import crud_account, crud_api_usage
from preloop.models.crud.history_policy import HISTORY_RETENTION_KEY
from preloop.models.crud.runtime_session_activity import crud_runtime_session_activity
from preloop.services import analytics_history as history
from preloop.services import retention_purge as purge
from preloop.services.retention_policy import CLASS_RUNTIME_SESSIONS, CLASS_USAGE


@pytest.fixture
def create_account(db_session):
    def create():
        return crud_account.create(
            db_session, obj_in={"organization_name": "history-policy-test"}
        )

    return create


@pytest.fixture
def policy(monkeypatch):
    provider = MagicMock(return_value=183)
    monkeypatch.setattr(
        history,
        "get_plugin_manager",
        lambda: SimpleNamespace(get_service=lambda _: provider),
    )
    return provider


@pytest.mark.parametrize("days", [183, 365, 730, 900])
def test_window_clamps_to_actual_now_not_an_old_requested_end(policy, days):
    policy.return_value = days
    now = datetime(2030, 9, 12, tzinfo=UTC)
    account = models.Account(meta_data={})
    start, end = history.restrict_history_window(
        MagicMock(),
        account=account,
        start_date=now - timedelta(days=1200),
        end_date=now - timedelta(days=1),
        now=now,
    )
    assert start == now - timedelta(days=days)
    assert end == now - timedelta(days=1)


def test_old_period_is_unavailable_not_zero_usage(policy):
    now = datetime(2030, 9, 12, tzinfo=UTC)
    with pytest.raises(HTTPException) as failure:
        history.restrict_history_window(
            MagicMock(),
            account=models.Account(meta_data={}),
            start_date=now - timedelta(days=800),
            end_date=now - timedelta(days=700),
            now=now,
        )
    assert failure.value.status_code == 403
    assert "cannot restore" in failure.value.detail["message"]


def test_oss_has_no_cloud_window(monkeypatch):
    monkeypatch.setattr(
        history,
        "get_plugin_manager",
        lambda: SimpleNamespace(get_service=lambda _: None),
    )
    start = datetime(2000, 1, 1, tzinfo=UTC)
    assert history.restrict_history_window(
        MagicMock(),
        account=models.Account(meta_data={}),
        start_date=start,
        end_date=None,
    ) == (start, None)


def test_unavailable_provider_cannot_choose_shorter_retention(policy):
    policy.side_effect = RuntimeError("database unavailable")
    with pytest.raises(HTTPException) as failure:
        history.storage_history_days(MagicMock(), account=models.Account(meta_data={}))
    assert failure.value.status_code == 503


@pytest.mark.parametrize(
    "promise,current,expected",
    [(730, 183, 730), (365, 730, 730), (-1, 183, None), (365, -1, None)],
)
def test_storage_preserves_longer_or_unlimited_promises(
    policy, promise, current, expected
):
    policy.return_value = current
    assert (
        history.storage_history_days(
            MagicMock(),
            account=models.Account(meta_data={HISTORY_RETENTION_KEY: promise}),
        )
        == expected
    )


def test_long_lived_session_keeps_recent_analytics(policy):
    now = datetime.now(UTC)
    history.require_session_history(
        MagicMock(),
        account=models.Account(meta_data={}),
        summary={"started_at": now - timedelta(days=900), "last_activity_at": now},
    )
    with pytest.raises(HTTPException):
        history.require_session_history(
            MagicMock(),
            account=models.Account(meta_data={}),
            summary={
                "started_at": now - timedelta(days=900),
                "last_activity_at": now - timedelta(days=800),
            },
        )


def make_session(db_session, account, now):
    session = models.RuntimeSession(
        account_id=account.id,
        session_source_type="test",
        session_source_id=str(uuid4()),
        started_at=now - timedelta(days=900),
        last_activity_at=now,
    )
    db_session.add(session)
    db_session.flush()
    return session


def make_usage(db_session, account, session, timestamp):
    row = models.ApiUsage(
        account_id=account.id,
        runtime_session_id=session.id,
        endpoint="/test",
        method="POST",
        status_code=200,
        duration=0.1,
        action_type="model_gateway",
        timestamp=timestamp,
        prompt_tokens=10,
        total_tokens=10,
        estimated_cost=0.1,
    )
    db_session.add(row)
    db_session.flush()
    return row


def test_mixed_session_rows_and_direct_old_ids_are_filtered(db_session, create_account):
    account = create_account()
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=183)
    session = make_session(db_session, account, now)
    old = make_usage(db_session, account, session, now - timedelta(days=200))
    recent = make_usage(db_session, account, session, now - timedelta(days=1))
    args = dict(account_id=account.id, runtime_session_id=session.id, start_date=cutoff)
    assert [
        row.id for row in crud_api_usage.list_session_request_rows(db_session, **args)
    ] == [recent.id]
    assert (
        crud_api_usage.list_session_request_rows(
            db_session, event_ids=[str(old.id)], **args
        )
        == []
    )
    assert crud_api_usage.count_session_request_rows(db_session, **args) == 1
    assert len(crud_api_usage.list_session_cache_rows(db_session, **args)) == 1
    for stamp in [old.timestamp, recent.timestamp]:
        db_session.add(
            models.RuntimeSessionActivity(
                account_id=account.id,
                runtime_session_id=session.id,
                activity_type="model_gateway_call",
                timestamp=stamp,
                metadata_={},
            )
        )
    db_session.flush()
    events = crud_runtime_session_activity.list_model_gateway_calls_for_session(
        db_session, **args
    )
    assert len(events) == 1
    assert (
        crud_runtime_session_activity.count_model_gateway_calls_for_session(
            db_session, **args
        )
        == 1
    )
    old_event = db_session.execute(
        select(models.RuntimeSessionActivity).where(
            models.RuntimeSessionActivity.runtime_session_id == session.id,
            models.RuntimeSessionActivity.timestamp < cutoff,
        )
    ).scalar_one()
    assert (
        crud_runtime_session_activity.get_model_gateway_call_for_session(
            db_session, activity_id=old_event.id, **args
        )
        is None
    )


def test_purge_retains_two_years_and_active_long_lived_session(
    policy, db_session, create_account
):
    policy.return_value = 730
    account = create_account()
    now = datetime.now(UTC)
    session = make_session(db_session, account, now)
    keep = make_usage(db_session, account, session, now - timedelta(days=500))
    expired = make_usage(db_session, account, session, now - timedelta(days=800))
    expired_id = expired.id
    result = purge.purge_class(
        db_session,
        account=account,
        record_class=CLASS_USAGE,
        now=now,
        batch_size=100,
        max_batches=1,
        dry_run=False,
    )
    assert result.retention_days == 730
    assert (
        db_session.execute(
            select(models.ApiUsage.id).where(models.ApiUsage.id == keep.id)
        ).scalar_one()
        == keep.id
    )
    assert (
        db_session.execute(
            select(models.ApiUsage.id).where(models.ApiUsage.id == expired_id)
        ).scalar_one_or_none()
        is None
    )
    result = purge.purge_class(
        db_session,
        account=account,
        record_class=CLASS_RUNTIME_SESSIONS,
        now=now,
        batch_size=100,
        max_batches=1,
        dry_run=False,
    )
    assert result.deleted == 0


def test_purge_respects_longer_account_policy_and_prior_plan_promise(
    policy, db_session, create_account
):
    policy.return_value = 183
    account = create_account()
    account.meta_data = {"retention": {"usage": 1000}, HISTORY_RETENTION_KEY: 730}
    result = purge.purge_class(
        db_session,
        account=account,
        record_class=CLASS_USAGE,
        now=datetime.now(UTC),
        batch_size=100,
        max_batches=1,
        dry_run=True,
    )
    assert result.retention_days == 1000


def test_standalone_purge_keeps_protected_metadata_without_plugin(
    monkeypatch, db_session, create_account
):
    monkeypatch.setattr(
        history,
        "get_plugin_manager",
        lambda: SimpleNamespace(get_service=lambda _: None),
    )
    account = create_account()
    account.meta_data = {HISTORY_RETENTION_KEY: 730}
    result = purge.purge_class(
        db_session,
        account=account,
        record_class=CLASS_USAGE,
        now=datetime.now(UTC),
        batch_size=100,
        max_batches=1,
        dry_run=True,
    )
    assert result.retention_days == 730


def test_api_requests_and_old_event_id_respect_history(
    policy, client, db_session, test_user
):
    account = db_session.get(models.Account, test_user.account_id)
    now = datetime.now(UTC)
    session = make_session(db_session, account, now)
    old = make_usage(db_session, account, session, now - timedelta(days=200))
    recent = make_usage(db_session, account, session, now - timedelta(days=1))
    event = models.RuntimeSessionActivity(
        account_id=account.id,
        runtime_session_id=session.id,
        activity_type="model_gateway_call",
        timestamp=old.timestamp,
        metadata_={"secret": "old"},
    )
    db_session.add(event)
    db_session.flush()
    response = client.get(f"/api/v1/runtime-sessions/{session.id}/requests")
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [str(recent.id)]
    response = client.get(
        f"/api/v1/runtime-sessions/{session.id}/requests?event_ids={old.id}"
    )
    assert response.status_code == 200 and response.json()["items"] == []
    assert (
        client.get(
            f"/api/v1/runtime-sessions/{session.id}/gateway-events/{event.id}"
        ).status_code
        == 404
    )


def test_retention_api_preview_matches_physical_plan_floor(
    policy, client, db_session, test_user
):
    policy.return_value = 730
    account = db_session.get(models.Account, test_user.account_id)
    now = datetime.now(UTC)
    session = make_session(db_session, account, now)
    make_usage(db_session, account, session, now - timedelta(days=500))
    response = client.get("/api/v1/retention/settings")
    assert response.status_code == 200
    usage = next(
        row for row in response.json()["classes"] if row["record_class"] == "usage"
    )
    assert usage["days"] == 730 and usage["source"] == "subscription_history"
    response = client.get("/api/v1/retention/purge-preview")
    assert response.status_code == 200
    usage = next(
        row for row in response.json()["classes"] if row["record_class"] == "usage"
    )
    assert usage["retention_days"] == 730 and usage["purgeable"] == 0


def test_standalone_worker_reads_persisted_plan_without_plugin(
    monkeypatch, db_session, create_account
):
    monkeypatch.setattr(
        history,
        "get_plugin_manager",
        lambda: SimpleNamespace(get_service=lambda _: None),
    )
    account = create_account()
    now = datetime.now(UTC)
    plan = models.Plan(
        id=str(uuid4()),
        name="history-" + str(uuid4()),
        features={"retention_days": 730},
    )
    db_session.add(plan)
    db_session.flush()
    db_session.add(
        models.Subscription(
            account_id=account.id,
            plan_id=plan.id,
            status="active",
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
        )
    )
    db_session.flush()
    assert history.storage_history_days(db_session, account=account) == 730


def test_analytics_window_does_not_block_live_control_or_old_audit_export(
    policy, client, db_session, test_user
):
    import io
    import json
    import tarfile

    account = db_session.get(models.Account, test_user.account_id)
    now = datetime.now(UTC)
    session = make_session(db_session, account, now)
    session.last_activity_at = now - timedelta(days=500)
    old = now - timedelta(days=500)
    db_session.add(
        models.AuditLog(
            account_id=account.id,
            action="history-test",
            status="success",
            timestamp=old,
        )
    )
    db_session.flush()
    response = client.patch(
        f"/api/v1/runtime-sessions/{session.id}", json={"action": "end"}
    )
    assert response.status_code == 200
    response = client.post(
        "/api/v1/retention/exports",
        params={
            "start": (old - timedelta(days=1)).date().isoformat(),
            "end": (old + timedelta(days=1)).date().isoformat(),
        },
    )
    assert response.status_code == 200
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as archive:
        manifest = json.load(archive.extractfile("manifest.json"))
    assert manifest["counts"]["audit"] == 1


def test_optimization_input_cache_and_job_records_obey_history(
    policy, client, db_session, test_user
):
    from preloop.models.crud import crud_runtime_session_optimization_result
    from preloop.services.context_analysis import load_session_gateway_events

    account = db_session.get(models.Account, test_user.account_id)
    now = datetime.now(UTC)
    session = make_session(db_session, account, now)
    cutoff = now - timedelta(days=183)
    for age in [200, 1]:
        db_session.add(
            models.RuntimeSessionActivity(
                account_id=account.id,
                runtime_session_id=session.id,
                activity_type="model_gateway_call",
                timestamp=now - timedelta(days=age),
                metadata_={"request": {"messages": []}},
            )
        )
        db_session.add(
            models.RuntimeSessionOptimizationResult(
                account_id=account.id,
                runtime_session_id=session.id,
                scope_hash=f"scope-{age}",
                response={"suggestions": []},
                updated_at=now - timedelta(days=age),
            )
        )
    db_session.flush()
    events = load_session_gateway_events(
        db_session, account=account, runtime_session_id=str(session.id)
    )
    assert len(events) == 1 and events[0].timestamp.replace(tzinfo=UTC) >= cutoff
    args = dict(account_id=account.id, start_date=cutoff)
    rows = crud_runtime_session_optimization_result.list_for_sessions(
        db_session, runtime_session_ids=[session.id], **args
    )
    assert [row.scope_hash for row in rows] == ["scope-1"]
    assert (
        crud_runtime_session_optimization_result.get_by_scope(
            db_session, runtime_session_id=session.id, scope_hash="scope-200", **args
        )
        is None
    )
    job = models.OptimizationJob(
        account_id=account.id,
        runtime_session_id=session.id,
        status="succeeded",
        result={"generated_by": "local", "suggestions": []},
        finished_at=now - timedelta(days=200),
    )
    db_session.add(job)
    db_session.flush()
    response = client.get(
        f"/api/v1/billing/cost/runtime-sessions/{session.id}/optimizations/jobs/{job.id}"
    )
    assert response.status_code == 404


@pytest.mark.parametrize("cloud_policy, deleted", [(None, 1), (-1, 0)])
def test_registered_off_policy_keeps_normal_purge_but_unlimited_protects(
    policy, db_session, create_account, cloud_policy, deleted
):
    policy.return_value = cloud_policy
    account = create_account()
    now = datetime.now(UTC)
    session = make_session(db_session, account, now)
    row = make_usage(db_session, account, session, now - timedelta(days=800))
    row_id = row.id
    result = purge.purge_class(
        db_session,
        account=account,
        record_class=CLASS_USAGE,
        now=now,
        batch_size=100,
        max_batches=1,
        dry_run=False,
    )
    assert result.deleted == deleted
    exists = db_session.execute(
        select(models.ApiUsage.id).where(models.ApiUsage.id == row_id)
    ).scalar_one_or_none()
    assert (exists is None) == bool(deleted)


@pytest.mark.parametrize("new_promise", [730, -1])
def test_purge_reloads_promise_after_each_committed_batch(
    monkeypatch, db_engine, new_promise
):
    """A second transaction can strengthen retention between deletion batches."""
    from sqlalchemy import delete
    from sqlalchemy.orm import Session
    from preloop.models.crud.history_policy import preserve_history_retention

    monkeypatch.setattr(
        history,
        "get_plugin_manager",
        lambda: SimpleNamespace(get_service=lambda _: None),
    )
    account_id = uuid4()
    first_id, protected_id = sorted([uuid4(), uuid4()])
    now = datetime.now(UTC)
    with Session(db_engine) as seed:
        seed.add(
            models.Account(
                id=account_id,
                organization_name="history-batch-race",
                meta_data={"retention": {"usage": 183}, HISTORY_RETENTION_KEY: 183},
            )
        )
        seed.flush()
        for identifier, age in [(first_id, 800), (protected_id, 500)]:
            seed.add(
                models.ApiUsage(
                    id=identifier,
                    account_id=account_id,
                    endpoint="/test",
                    method="POST",
                    status_code=200,
                    duration=0.1,
                    action_type="model_gateway",
                    timestamp=now - timedelta(days=age),
                )
            )
        seed.commit()
    try:
        with Session(db_engine, expire_on_commit=False) as purger:
            stale = purger.get(models.Account, account_id)
            original_commit = purger.commit
            first_batch = True

            def commit_then_strengthen():
                nonlocal first_batch
                original_commit()
                if first_batch:
                    first_batch = False
                    with Session(db_engine) as writer:
                        preserve_history_retention(
                            writer, account_id=account_id, days=new_promise
                        )
                        writer.commit()

            monkeypatch.setattr(purger, "commit", commit_then_strengthen)
            result = purge.purge_class(
                purger,
                account=stale,
                record_class=CLASS_USAGE,
                now=now,
                batch_size=1,
                max_batches=3,
                dry_run=False,
            )
            assert result.deleted == 1
            assert result.retention_days == new_promise
            assert result.applied_cutoffs[0]["retention_days"] == 183
        with Session(db_engine) as check:
            assert check.get(models.ApiUsage, first_id) is None
            assert check.get(models.ApiUsage, protected_id) is not None
    finally:
        with Session(db_engine) as cleanup:
            cleanup.execute(
                delete(models.ApiUsage).where(models.ApiUsage.account_id == account_id)
            )
            cleanup.execute(
                delete(models.Account).where(models.Account.id == account_id)
            )
            cleanup.commit()


def test_later_activity_protects_even_a_previously_ended_session(
    policy, db_session, create_account
):
    policy.return_value = 730
    account = create_account()
    now = datetime.now(UTC)
    session = make_session(db_session, account, now)
    session.ended_at = now - timedelta(days=800)
    db_session.flush()
    result = purge.purge_class(
        db_session,
        account=account,
        record_class=CLASS_RUNTIME_SESSIONS,
        now=now,
        batch_size=100,
        max_batches=1,
        dry_run=False,
    )
    assert result.deleted == 0


@pytest.mark.parametrize("status", ["past_due", "unpaid", "canceled"])
@pytest.mark.parametrize(
    "features,expected",
    [
        ({"retention_days": 730}, 730),
        ({"audit_logs_retention_days": 900}, 900),
        ({"audit_logs_retention_days": -1}, -1),
        ({"retention_days": 730, "audit_logs_retention_days": -1}, 730),
    ],
)
def test_purge_preserves_unmaterialized_plan_promise_even_with_free_access(
    policy, db_session, create_account, status, features, expected
):
    """A reporting downgrade cannot silently erase a persisted storage promise."""
    account = create_account()
    now = datetime.now(UTC)
    plan_id = "history-" + uuid4().hex
    db_session.add(models.Plan(id=plan_id, name=plan_id, features=features))
    db_session.flush()
    db_session.add(
        models.Subscription(
            account_id=account.id,
            plan_id=plan_id,
            status=status,
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
        )
    )
    session = make_session(db_session, account, now)
    keep_id = make_usage(db_session, account, session, now - timedelta(days=500)).id
    expired_id = make_usage(db_session, account, session, now - timedelta(days=1000)).id
    db_session.commit()
    assert account.subscription_history_retention_days is None
    assert history.history_cutoff(
        db_session, account=account, now=now
    ) == now - timedelta(days=183)
    result = purge.purge_class(
        db_session,
        account=account,
        record_class=CLASS_USAGE,
        now=now,
        batch_size=100,
        max_batches=1,
        dry_run=False,
    )
    assert result.retention_days == expected
    remaining = set(
        db_session.execute(
            select(models.ApiUsage.id).where(
                models.ApiUsage.id.in_([keep_id, expired_id])
            )
        ).scalars()
    )
    assert keep_id in remaining
    assert (expired_id in remaining) == (expected == -1)


def test_a_shorter_display_window_does_not_shorten_storage(monkeypatch):
    """Reporting may show less than the plan keeps; the purge follows storage."""
    services = {
        "analytics_history_policy": MagicMock(return_value=90),
        "analytics_storage_policy": MagicMock(return_value=183),
    }
    monkeypatch.setattr(
        history,
        "get_plugin_manager",
        lambda: SimpleNamespace(get_service=services.get),
    )
    account = models.Account(meta_data={})
    assert history.analytics_history_days(MagicMock(), account=account) == 90
    assert history.storage_history_days(MagicMock(), account=account) == 183


def test_storage_falls_back_to_the_reporting_policy(monkeypatch):
    """A plugin build that publishes only the reporting policy is unchanged."""
    provider = MagicMock(return_value=365)
    services = {"analytics_history_policy": provider}
    monkeypatch.setattr(
        history,
        "get_plugin_manager",
        lambda: SimpleNamespace(get_service=services.get),
    )
    assert (
        history.storage_history_days(MagicMock(), account=models.Account(meta_data={}))
        == 365
    )


def test_oss_has_no_storage_policy(monkeypatch):
    monkeypatch.setattr(
        history,
        "get_plugin_manager",
        lambda: SimpleNamespace(get_service=lambda _: None),
    )
    assert (
        history.storage_history_days(MagicMock(), account=models.Account(meta_data={}))
        == 0
    )


@pytest.mark.parametrize("days", [0, -5, 89, True, "183"])
def test_a_broken_policy_cannot_shrink_the_window(monkeypatch, days):
    provider = MagicMock(return_value=days)
    monkeypatch.setattr(
        history,
        "get_plugin_manager",
        lambda: SimpleNamespace(get_service=lambda _: provider),
    )
    with pytest.raises(HTTPException) as failure:
        history.analytics_history_days(
            MagicMock(), account=models.Account(meta_data={})
        )
    assert failure.value.status_code == 503

"""Core budget API enforces owner permissions and tenant isolation without EE."""

from uuid import uuid4
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import budget
from preloop.models import models
from preloop.models.crud import crud_account, crud_user
from preloop.models.db.session import get_db_session
from preloop.services.configuration_gating import register_configuration_authorizer


@pytest.fixture
def budget_client(db_session, monkeypatch):
    from preloop.utils import permissions

    monkeypatch.setattr(permissions, "_plugin_require_permission", None)
    register_configuration_authorizer(None)
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Core budgets"}
    )
    user = crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "username": "budget-" + uuid4().hex,
            "email": uuid4().hex + "@example.test",
            "hashed_password": "test",
            "is_active": True,
        },
    )
    crud_account.update(db_session, db_obj=account, obj_in={"primary_user_id": user.id})
    app = FastAPI()
    app.include_router(budget.router, prefix="/api/v1")
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: user
    with TestClient(app) as client:
        yield client, account, user
    register_configuration_authorizer(None)


def test_oss_owner_creates_reads_updates_and_removes_basic_zero_budget(budget_client):
    client, _, _ = budget_client
    response = client.post(
        "/api/v1/budget/policies",
        json={"subject_type": "account", "period": "monthly", "hard_limit_usd": 0},
    )
    assert response.status_code == 200, response.text
    policy_id = response.json()["id"]
    assert response.json()["hard_limit_usd"] == 0
    assert len(client.get("/api/v1/budget/policies").json()) == 1
    changed = client.put(
        "/api/v1/budget/policies/" + policy_id, json={"hard_limit_usd": None}
    )
    assert changed.status_code == 200 and changed.json()["hard_limit_usd"] is None
    deleted = client.delete("/api/v1/budget/policies/" + policy_id)
    assert deleted.status_code == 200
    assert client.get("/api/v1/budget/policies").json() == []


def test_oss_member_cannot_raise_or_delete_budget(budget_client, db_session):
    client, account, user = budget_client
    response = client.post(
        "/api/v1/budget/policies",
        json={"subject_type": "account", "period": "monthly", "hard_limit_usd": 1},
    )
    policy_id = response.json()["id"]
    crud_account.update(db_session, db_obj=account, obj_in={"primary_user_id": None})
    assert (
        client.post(
            "/api/v1/budget/policies",
            json={"subject_type": "account", "period": "daily", "hard_limit_usd": 100},
        ).status_code
        == 403
    )
    assert (
        client.put(
            "/api/v1/budget/policies/" + policy_id, json={"hard_limit_usd": 100}
        ).status_code
        == 403
    )
    deleted = client.delete("/api/v1/budget/policies/" + policy_id)
    assert deleted.status_code == 403


def test_foreign_policy_and_subject_are_not_accessible(budget_client, db_session):
    client, _, _ = budget_client
    other = crud_account.create(db_session, obj_in={"organization_name": "Other"})
    foreign = models.BudgetPolicy(
        account_id=other.id,
        subject_type="account",
        period=models.BudgetPeriod.monthly,
        hard_limit_usd=5,
    )
    foreign_model = models.AIModel(
        account_id=other.id,
        name="Private model",
        provider_name="openai",
        model_identifier="private",
    )
    db_session.add_all([foreign, foreign_model])
    db_session.commit()
    assert client.get("/api/v1/budget/policies").json() == []
    assert (
        client.put(
            "/api/v1/budget/policies/" + str(foreign.id), json={"hard_limit_usd": 100}
        ).status_code
        == 404
    )
    deleted = client.delete("/api/v1/budget/policies/" + str(foreign.id))
    assert deleted.status_code == 404
    rejected = client.post(
        "/api/v1/budget/policies",
        json={
            "subject_type": "ai_model",
            "subject_id": str(foreign_model.id),
            "period": "monthly",
            "hard_limit_usd": 1,
        },
    )
    assert rejected.status_code == 400


@pytest.mark.parametrize(
    "payload",
    [{"hard_limit_usd": -1}, {"hard_limit_usd": "NaN"}, {"hard_limit_usd": "Infinity"}],
)
def test_nonfinite_or_negative_limits_are_rejected(budget_client, payload):
    client, _, _ = budget_client
    assert (
        client.post(
            "/api/v1/budget/policies",
            json={"subject_type": "account", "period": "monthly", **payload},
        ).status_code
        == 422
    )


def test_oss_advanced_budget_routing_is_explicitly_commercial(budget_client):
    client, _, user = budget_client
    assert (
        client.post(
            "/api/v1/budget/policies",
            json={
                "subject_type": "account",
                "period": "monthly",
                "hard_limit_usd": 1,
                "notify_on_hard": True,
            },
        ).status_code
        == 402
    )
    assert (
        client.post(
            "/api/v1/budget/policies",
            json={
                "subject_type": "user",
                "subject_id": str(user.id),
                "period": "monthly",
                "hard_limit_usd": 1,
            },
        ).status_code
        == 402
    )


@pytest.mark.parametrize("subject_type", ["api_key", "managed_agent", "ai_model"])
def test_each_basic_subject_can_be_configured_in_oss(
    budget_client, db_session, subject_type
):
    from preloop.models.crud import crud_managed_agent

    client, account, user = budget_client
    if subject_type == "managed_agent":
        subject = crud_managed_agent.create_custom_agent(
            db_session, account_id=account.id, display_name="Basic agent"
        )
    elif subject_type == "api_key":
        subject = models.ApiKey(
            account_id=account.id, user_id=user.id, name="Basic key"
        )
        db_session.add(subject)
        db_session.commit()
    else:
        subject = models.AIModel(
            account_id=account.id,
            name="Basic model",
            provider_name="openai",
            model_identifier="test",
            meta_data={"gateway": {"enabled": True, "model_alias": "canonical-test"}},
        )
        db_session.add(subject)
        db_session.commit()
    response = client.post(
        "/api/v1/budget/policies",
        json={
            "subject_type": subject_type,
            "subject_id": str(subject.id),
            "period": "daily",
            "hard_limit_usd": 1,
        },
    )
    assert response.status_code == 200, response.text
    if subject_type == "ai_model":
        assert response.json()["subject_type"] == "account"
        assert response.json()["subject_id"] is None
        assert response.json()["model_alias"] == "canonical-test"
        listed = client.get(
            "/api/v1/budget/policies",
            params={"subject_type": "ai_model", "subject_id": str(subject.id)},
        )
        assert [row["id"] for row in listed.json()] == [response.json()["id"]]
    else:
        assert response.json()["subject_id"] == str(subject.id)


def test_advanced_notifications_cannot_reference_another_account(budget_client):
    client, account, user = budget_client
    register_configuration_authorizer(lambda *_: None)
    response = client.post(
        "/api/v1/budget/policies",
        json={
            "subject_type": "account",
            "period": "daily",
            "hard_limit_usd": 1,
            "notify_on_hard": True,
            "notification_user_ids": [str(uuid4())],
        },
    )
    assert response.status_code == 400


@pytest.mark.parametrize("scope", ["flow", "team"])
def test_unenforced_budget_scopes_are_rejected(budget_client, scope):
    client, _, _ = budget_client
    register_configuration_authorizer(lambda *_: None)
    response = client.post(
        "/api/v1/budget/policies",
        json={
            "subject_type": scope,
            "subject_id": str(uuid4()),
            "period": "daily",
            "hard_limit_usd": 1,
        },
    )
    assert response.status_code == 400


def test_existing_model_policy_is_normalized_on_update(budget_client, db_session):
    client, account, _ = budget_client
    model = models.AIModel(
        account_id=account.id,
        name="Display name",
        provider_name="openai",
        model_identifier="test",
        meta_data={"gateway": {"enabled": True, "model_alias": " canonical-test "}},
    )
    db_session.add(model)
    db_session.flush()
    policy = models.BudgetPolicy(
        account_id=account.id,
        subject_type="ai_model",
        subject_id=model.id,
        period=models.BudgetPeriod.monthly,
        hard_limit_usd=1,
    )
    db_session.add(policy)
    db_session.commit()
    response = client.put(
        "/api/v1/budget/policies/" + str(policy.id), json={"hard_limit_usd": 0}
    )
    assert response.status_code == 200, response.text
    assert response.json()["subject_type"] == "account"
    assert response.json()["subject_id"] is None
    assert response.json()["model_alias"] == "canonical-test"


@pytest.mark.parametrize("subject_type", ["account", "api_key"])
@pytest.mark.parametrize(
    "alias_state", ["unknown", "disabled", "foreign", "enabled", "system"]
)
def test_explicit_alias_must_resolve_to_an_available_enabled_gateway(
    budget_client, db_session, subject_type, alias_state
):
    client, account, user = budget_client
    owner = account.id
    if alias_state == "foreign":
        owner = crud_account.create(
            db_session, obj_in={"organization_name": "Other alias owner"}
        ).id
    elif alias_state == "system":
        owner = None
    if alias_state != "unknown":
        db_session.add(
            models.AIModel(
                account_id=owner,
                name="Configured model",
                provider_name="openai",
                model_identifier="test-alias-model",
                meta_data={
                    "gateway": {
                        "enabled": alias_state != "disabled",
                        "model_alias": " canonical-alias ",
                    }
                },
            )
        )
    subject_id = None
    if subject_type == "api_key":
        key = models.ApiKey(account_id=account.id, user_id=user.id, name="Alias key")
        db_session.add(key)
        db_session.flush()
        subject_id = str(key.id)
    db_session.commit()
    response = client.post(
        "/api/v1/budget/policies",
        json={
            "subject_type": subject_type,
            "subject_id": subject_id,
            "model_alias": " canonical-alias ",
            "period": "daily",
            "hard_limit_usd": 1,
        },
    )
    if alias_state in {"enabled", "system"}:
        assert response.status_code == 200, response.text
        assert response.json()["model_alias"] == "canonical-alias"
    else:
        assert response.status_code == 400
        assert (
            db_session.query(models.BudgetPolicy)
            .filter_by(account_id=account.id)
            .count()
            == 0
        )


@pytest.mark.parametrize("params", [{}, {"subject_type": "account"}])
def test_policy_list_batches_current_period_spend_query(
    budget_client, db_session, params
):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import event

    client, account, _ = budget_client
    now = datetime.now(timezone.utc)
    for period, amount in [
        (models.BudgetPeriod.daily, 2),
        (models.BudgetPeriod.monthly, 3),
        (models.BudgetPeriod.all_time, 4),
    ]:
        db_session.add(
            models.BudgetPolicy(
                account_id=account.id,
                subject_type="account",
                period=period,
                hard_limit_usd=10,
            )
        )
        db_session.add(
            models.BudgetSpendActivity(
                account_id=account.id,
                subject_type="account",
                period=period,
                period_start=budget.get_period_start(now, period),
                spend_usd=amount,
            )
        )
    db_session.add(
        models.BudgetSpendActivity(
            account_id=account.id,
            subject_type="account",
            period=models.BudgetPeriod.daily,
            period_start=budget.get_period_start(
                now - timedelta(days=1), models.BudgetPeriod.daily
            ),
            spend_usd=99,
        )
    )
    db_session.commit()
    statements = []

    def record_query(conn, cursor, statement, parameters, context, executemany):
        if (
            statement.lstrip().upper().startswith("SELECT")
            and "budget_spend_activities" in statement
        ):
            statements.append(statement)

    event.listen(db_session.bind, "before_cursor_execute", record_query)
    try:
        response = client.get("/api/v1/budget/policies", params=params)
    finally:
        event.remove(db_session.bind, "before_cursor_execute", record_query)
    assert response.status_code == 200, response.text
    assert {row["period"]: row["current_spend_usd"] for row in response.json()} == {
        "daily": 2,
        "monthly": 3,
        "all_time": 4,
    }
    assert len(statements) == 1


def test_unavailable_batched_spend_stays_unknown(budget_client, monkeypatch):
    client, _, _ = budget_client
    created = client.post(
        "/api/v1/budget/policies",
        json={"subject_type": "account", "period": "daily", "hard_limit_usd": 1},
    )
    assert created.status_code == 200

    def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic query unavailable")

    monkeypatch.setattr(budget.crud_budget_spend, "get_spend_multi", unavailable)
    response = client.get("/api/v1/budget/policies")
    assert response.status_code == 200
    assert response.json()[0]["current_spend_usd"] is None


def test_stale_alias_policy_remains_readable_without_rewriting_spend(
    budget_client, db_session
):
    from datetime import datetime, timezone

    client, account, _ = budget_client
    policy = models.BudgetPolicy(
        account_id=account.id,
        subject_type="account",
        model_alias="former-alias",
        period=models.BudgetPeriod.monthly,
        hard_limit_usd=10,
    )
    db_session.add(policy)
    db_session.add(
        models.BudgetSpendActivity(
            account_id=account.id,
            subject_type="account",
            model_alias="former-alias",
            period=models.BudgetPeriod.monthly,
            period_start=budget.get_period_start(
                datetime.now(timezone.utc), models.BudgetPeriod.monthly
            ),
            spend_usd=5,
        )
    )
    db_session.commit()
    response = client.get("/api/v1/budget/policies", params={"subject_type": "account"})
    assert response.status_code == 200
    assert response.json()[0]["model_alias"] == "former-alias"
    assert response.json()[0]["current_spend_usd"] == 5
    db_session.refresh(policy)
    assert policy.model_alias == "former-alias"

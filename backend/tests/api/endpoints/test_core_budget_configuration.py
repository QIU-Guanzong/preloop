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
    assert client.delete("/api/v1/budget/policies/" + policy_id).status_code == 200
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
    assert client.delete("/api/v1/budget/policies/" + policy_id).status_code == 403


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
    assert (
        client.delete("/api/v1/budget/policies/" + str(foreign.id)).status_code == 404
    )
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

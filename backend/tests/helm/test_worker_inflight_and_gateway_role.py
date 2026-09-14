"""Guards for flow-execution inflight, DB pool, and dedicated service roles."""

from __future__ import annotations

from typing import Dict, List

import yaml

from tests.helm.chart_helpers import helm_template, load_values, resolve_values_path


def _docs(rendered: str) -> List[Dict]:
    return [doc for doc in yaml.safe_load_all(rendered) if doc]


def _container_env(
    rendered: str, *, name_contains: str | None = None
) -> Dict[str, Dict]:
    for doc in _docs(rendered):
        if doc.get("kind") != "Deployment":
            continue
        meta = doc.get("metadata") or {}
        if name_contains and name_contains not in str(meta.get("name", "")):
            continue
        env = doc["spec"]["template"]["spec"]["containers"][0].get("env") or []
        return {item["name"]: item for item in env}
    raise AssertionError(f"no Deployment matching {name_contains!r}")


def test_values_default_flow_inflight_is_ten() -> None:
    values = load_values()
    assert resolve_values_path(values, "flowExecution.maxInflight") == 10
    assert resolve_values_path(values, "flowExecution.databasePool.size") == 10
    assert resolve_values_path(values, "flowExecution.databasePool.maxOverflow") == 4


def test_flow_execution_worker_sets_inflight_and_pool() -> None:
    env = _container_env(
        helm_template("templates/spacesync-worker-deployment.yaml"),
        name_contains="flow-execution",
    )
    assert env["FLOW_EXECUTION_MAX_INFLIGHT"]["value"] == "10"
    assert env["DATABASE_POOL_SIZE"]["value"] == "10"
    assert env["DATABASE_MAX_OVERFLOW"]["value"] == "4"


def test_default_worker_keeps_small_pool_and_no_inflight_env() -> None:
    env = _container_env(
        helm_template("templates/spacesync-worker-deployment.yaml"),
        name_contains="default",
    )
    assert "FLOW_EXECUTION_MAX_INFLIGHT" not in env
    assert env["DATABASE_POOL_SIZE"]["value"] == "2"
    assert env["DATABASE_MAX_OVERFLOW"]["value"] == "4"


def test_gateway_and_api_set_service_roles() -> None:
    gateway = _container_env(helm_template("templates/gateway-deployment.yaml"))
    api = _container_env(helm_template("templates/api-deployment.yaml"))
    assert gateway["PRELOOP_SERVICE_ROLE"]["value"] == "gateway"
    assert api["PRELOOP_SERVICE_ROLE"]["value"] == "api"


def test_gateway_memory_request_is_honest() -> None:
    values = load_values()
    assert resolve_values_path(values, "gateway.resources.requests.memory") == "768Mi"
    assert resolve_values_path(values, "gateway.resources.limits.memory") == "2Gi"
    assert resolve_values_path(values, "gateway.autoscaling.minReplicas") == 2
    assert resolve_values_path(values, "gateway.autoscaling.maxReplicas") == 8
    assert (
        resolve_values_path(
            values, "gateway.autoscaling.targetMemoryUtilizationPercentage"
        )
        == 90
    )


def test_gateway_hpa_uses_gateway_autoscaling_not_api() -> None:
    rendered = yaml.safe_load(helm_template("templates/gateway-hpa.yaml"))
    assert rendered["spec"]["minReplicas"] == 2
    assert rendered["spec"]["maxReplicas"] == 8
    gateway = yaml.safe_load(helm_template("templates/gateway-deployment.yaml"))
    assert "replicas" not in gateway["spec"]

"""Guards for the in-cluster model gateway URL the chart hands to pods.

A split deployment runs the API pods with ``PRELOOP_SERVICE_ROLE=api``, and
those pods never mount ``/openai/v1``. An agent Job told to call the API
Service therefore gets ``404 Not Found`` for every model call, which is how a
flow run fails with no useful signal. The env var below is what keeps that
from happening, so it is asserted rather than assumed.
"""

from __future__ import annotations

from typing import Dict, Iterator, List

import yaml

from tests.helm.chart_helpers import helm_template, load_values, resolve_values_path

GATEWAY_URL_ENV = "PRELOOP_MODEL_GATEWAY_URL_K8S"
EXPECTED_DEFAULT = "http://preloop-gateway:80/openai/v1"


def _docs(rendered: str) -> List[Dict]:
    return [doc for doc in yaml.safe_load_all(rendered) if doc]


def _deployment_envs(rendered: str) -> Iterator[tuple[str, Dict[str, Dict]]]:
    for doc in _docs(rendered):
        if doc.get("kind") != "Deployment":
            continue
        name = str((doc.get("metadata") or {}).get("name", ""))
        env = doc["spec"]["template"]["spec"]["containers"][0].get("env") or []
        yield name, {item["name"]: item for item in env}


def _single_env(rendered: str) -> Dict[str, Dict]:
    envs = list(_deployment_envs(rendered))
    assert len(envs) == 1, [name for name, _ in envs]
    return envs[0][1]


def test_values_leave_the_in_cluster_url_to_the_chart() -> None:
    values = load_values()
    assert resolve_values_path(values, "gateway.inClusterUrl") == ""


def test_api_pod_points_agents_at_the_gateway_service() -> None:
    env = _single_env(helm_template("templates/api-deployment.yaml"))
    assert env[GATEWAY_URL_ENV]["value"] == EXPECTED_DEFAULT
    # The same pod still knows the API Service; the two must not be confused.
    assert env["PRELOOP_API_SERVICE_HTTP_ENDPOINT"]["value"] == "http://preloop-api:80"


def test_the_default_names_the_service_the_chart_actually_deploys() -> None:
    services = [
        doc
        for doc in _docs(helm_template("templates/gateway-service.yaml"))
        if doc.get("kind") == "Service"
    ]
    assert len(services) == 1
    service = services[0]
    host, _, port = EXPECTED_DEFAULT.split("//", 1)[1].split("/", 1)[0].partition(":")
    assert service["metadata"]["name"] == host
    assert int(port) in {entry["port"] for entry in service["spec"]["ports"]}


def test_every_worker_pool_launches_agents_with_the_gateway_url() -> None:
    envs = list(
        _deployment_envs(helm_template("templates/spacesync-worker-deployment.yaml"))
    )
    assert envs, "no worker Deployments rendered"
    for name, env in envs:
        assert env[GATEWAY_URL_ENV]["value"] == EXPECTED_DEFAULT, name


def test_operators_can_override_the_in_cluster_url() -> None:
    overrides = ["gateway.inClusterUrl=http://models.internal:8080/openai/v1"]
    api = _single_env(helm_template("templates/api-deployment.yaml", overrides))
    assert api[GATEWAY_URL_ENV]["value"] == "http://models.internal:8080/openai/v1"
    for name, env in _deployment_envs(
        helm_template("templates/spacesync-worker-deployment.yaml", overrides)
    ):
        assert (
            env[GATEWAY_URL_ENV]["value"] == "http://models.internal:8080/openai/v1"
        ), name

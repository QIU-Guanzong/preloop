"""Guards for agent pod isolation.

Agent containers run model-authored code. The chart's job is to make sure
such a pod cannot reach the database, the message bus, or the cloud metadata
endpoint, in both supported layouts (agents in their own namespace, or agents
sharing the release namespace), in the plain NetworkPolicy and in the
CiliumNetworkPolicy variant, and across an upgrade from the chart version
that used the previous value names.
"""

from __future__ import annotations

from typing import Dict, List

import yaml

from tests.helm.chart_helpers import (
    CHART_DIR,
    helm_template,
    helm_template_all,
    load_values,
)

POLICY_TEMPLATE = "templates/agent-networkpolicy.yaml"
CILIUM_TEMPLATE = "templates/agent-ciliumnetworkpolicy.yaml"
SEPARATE = "agentExecution.namespace.create=true"
SHARED = "agentExecution.namespace.create=false"
CILIUM = "agentExecution.networkPolicy.cilium.enabled=true"

PRIVATE_RANGES = {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"}
METADATA_ADDRESS = "169.254.169.254/32"


def _docs(rendered: str) -> List[Dict]:
    return [doc for doc in yaml.safe_load_all(rendered) if doc]


def _agent_policy(overrides: List[str] | None = None) -> Dict:
    """The policy that selects app=agent-execution pods (not the namespace deny)."""
    rendered = helm_template(POLICY_TEMPLATE, overrides=overrides)
    policies = [
        doc
        for doc in _docs(rendered)
        if doc["kind"] == "NetworkPolicy"
        and doc["spec"]["podSelector"] == {"matchLabels": {"app": "agent-execution"}}
    ]
    assert len(policies) == 1, "expected exactly one agent NetworkPolicy"
    return policies[0]


def _cilium_policy(overrides: List[str] | None = None) -> Dict:
    rendered = helm_template(CILIUM_TEMPLATE, overrides=[CILIUM] + (overrides or []))
    policies = [doc for doc in _docs(rendered) if doc["kind"] == "CiliumNetworkPolicy"]
    assert len(policies) == 1, "expected exactly one CiliumNetworkPolicy"
    return policies[0]


def _peers(policy: Dict) -> List[Dict]:
    return [peer for rule in policy["spec"]["egress"] for peer in rule.get("to", [])]


def _ip_blocks(policy: Dict) -> List[Dict]:
    return [peer["ipBlock"] for peer in _peers(policy) if "ipBlock" in peer]


def _ports(policy: Dict) -> List[int]:
    return [
        port.get("port")
        for rule in policy["spec"]["egress"]
        for port in rule.get("ports", [])
    ]


def _control_plane_rule(policy: Dict) -> Dict | None:
    for rule in policy["spec"]["egress"]:
        for peer in rule.get("to", []):
            labels = peer.get("podSelector", {}).get("matchLabels", {})
            if labels.get("app.kubernetes.io/component") in {"api", "gateway"}:
                return rule
    return None


# --- defaults ---------------------------------------------------------------


def test_policy_is_enabled_by_default() -> None:
    """A chart that ships the policy off is a chart nobody turns on."""
    network_policy = load_values()["agentExecution"]["networkPolicy"]
    assert network_policy["enabled"] is True
    assert network_policy["cilium"]["enabled"] is False


def test_new_cidr_key_ships_empty_so_the_legacy_key_can_still_be_read() -> None:
    """Helm lays saved values over chart defaults.

    A populated ``clusterCidrs`` default would therefore shadow a saved
    ``excludeCIDRs`` on every upgrade, silently dropping the carve-out of
    exactly the installs that had customised it.
    """
    network_policy = load_values()["agentExecution"]["networkPolicy"]
    assert network_policy["clusterCidrs"] == []
    assert network_policy["excludeCIDRs"] == []
    assert network_policy["extraEgress"] == []
    assert network_policy["additionalEgressRules"] == []


# --- both layouts render ----------------------------------------------------


def test_shared_namespace_selects_agent_pods_in_the_release_namespace() -> None:
    policy = _agent_policy([SHARED])
    assert policy["metadata"]["namespace"] == "default"
    assert policy["spec"]["podSelector"] == {"matchLabels": {"app": "agent-execution"}}


def test_separate_namespace_adds_a_namespace_wide_deny() -> None:
    rendered = helm_template(POLICY_TEMPLATE, overrides=[SEPARATE])
    policies = {
        doc["metadata"]["name"]: doc
        for doc in _docs(rendered)
        if doc["kind"] == "NetworkPolicy"
    }
    agent = policies["preloop-agent-execution"]
    deny = policies["agent-execution-isolation"]
    assert agent["metadata"]["namespace"] == "agent-executions"
    assert deny["metadata"]["namespace"] == "agent-executions"
    assert deny["spec"]["podSelector"] == {}
    assert deny["spec"]["ingress"] == []
    assert deny["spec"]["egress"] == []


def test_both_layouts_deny_ingress_and_restrict_egress() -> None:
    for overrides in ([SHARED], [SEPARATE]):
        policy = _agent_policy(overrides)
        assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
        assert policy["spec"]["ingress"] == []
        assert policy["spec"]["egress"], "egress must not be empty"


def test_both_layouts_allow_dns_pods_only() -> None:
    for overrides in ([SHARED], [SEPARATE]):
        policy = _agent_policy(overrides)
        dns = policy["spec"]["egress"][0]
        peer = dns["to"][0]
        assert peer["namespaceSelector"]["matchLabels"] == {
            "kubernetes.io/metadata.name": "kube-system"
        }
        assert peer["podSelector"]["matchLabels"] == {"k8s-app": "kube-dns"}
        assert {"protocol": "UDP", "port": 53} in dns["ports"]
        assert {"protocol": "TCP", "port": 53} in dns["ports"]


def test_dns_pod_selector_can_be_widened_to_the_namespace() -> None:
    policy = _agent_policy(
        [SHARED, "agentExecution.networkPolicy.dns.podSelectorLabels=null"]
    )
    peer = policy["spec"]["egress"][0]["to"][0]
    assert "podSelector" not in peer


def test_both_layouts_allow_api_and_gateway_pods_on_both_ports() -> None:
    """80 is the Service port, 8000 the container port the CNI sees after DNAT."""
    for overrides in ([SHARED], [SEPARATE]):
        policy = _agent_policy(overrides)
        rule = _control_plane_rule(policy)
        assert rule is not None
        components = sorted(
            peer["podSelector"]["matchLabels"]["app.kubernetes.io/component"]
            for peer in rule["to"]
        )
        assert components == ["api", "gateway"]
        for peer in rule["to"]:
            assert peer["namespaceSelector"]["matchLabels"] == {
                "kubernetes.io/metadata.name": "default"
            }
            assert (
                peer["podSelector"]["matchLabels"]["app.kubernetes.io/name"]
                == "preloop"
            )
        assert sorted(port["port"] for port in rule["ports"]) == [80, 8000]


# --- what must stay unreachable --------------------------------------------


def test_public_egress_cuts_out_private_space_and_metadata_by_default() -> None:
    for overrides in ([SHARED], [SEPARATE]):
        blocks = _ip_blocks(_agent_policy(overrides))
        assert len(blocks) == 1
        assert blocks[0]["cidr"] == "0.0.0.0/0"
        excepted = set(blocks[0]["except"])
        assert PRIVATE_RANGES <= excepted
        assert METADATA_ADDRESS in excepted


def test_no_peer_opens_the_database_or_message_bus() -> None:
    for overrides in ([SHARED], [SEPARATE]):
        policy = _agent_policy(overrides)
        for peer in _peers(policy):
            labels = peer.get("podSelector", {}).get("matchLabels", {})
            assert labels.get("app.kubernetes.io/component") not in {
                "console",
                "database",
                "nats",
            }
        assert 5432 not in _ports(policy)
        assert 4222 not in _ports(policy)


# --- upgrade compatibility --------------------------------------------------


def test_legacy_exclude_cidrs_survive_the_upgrade_unchanged() -> None:
    """The finding this file exists for: a customised carve-out must not vanish."""
    policy = _agent_policy(
        [SHARED, "agentExecution.networkPolicy.excludeCIDRs={100.64.0.0/10}"]
    )
    assert _ip_blocks(policy)[0]["except"] == ["100.64.0.0/10"]


def test_cluster_cidrs_win_over_the_legacy_key_when_both_are_set() -> None:
    policy = _agent_policy(
        [
            SHARED,
            "agentExecution.networkPolicy.clusterCidrs={10.244.0.0/16,10.96.0.0/12}",
            "agentExecution.networkPolicy.excludeCIDRs={100.64.0.0/10}",
        ]
    )
    assert _ip_blocks(policy)[0]["except"] == ["10.244.0.0/16", "10.96.0.0/12"]


def test_no_cidr_value_at_all_falls_back_instead_of_failing() -> None:
    policy = _agent_policy(
        [
            SHARED,
            "agentExecution.networkPolicy.clusterCidrs=null",
            "agentExecution.networkPolicy.excludeCIDRs=null",
        ]
    )
    assert set(_ip_blocks(policy)[0]["except"]) == PRIVATE_RANGES | {METADATA_ADDRESS}


def test_legacy_additional_egress_rules_are_still_appended() -> None:
    policy = _agent_policy(
        [
            SHARED,
            "agentExecution.networkPolicy.additionalEgressRules[0].to[0]"
            ".ipBlock.cidr=10.42.7.0/24",
        ]
    )
    assert policy["spec"]["egress"][-1] == {
        "to": [{"ipBlock": {"cidr": "10.42.7.0/24"}}]
    }


# --- knobs ------------------------------------------------------------------


def test_extra_egress_is_appended_verbatim() -> None:
    for overrides in ([SHARED], [SEPARATE]):
        policy = _agent_policy(
            overrides
            + [
                "agentExecution.networkPolicy.extraEgress[0].to[0].ipBlock.cidr=10.42.7.0/24",
                "agentExecution.networkPolicy.extraEgress[0].ports[0].protocol=TCP",
                "agentExecution.networkPolicy.extraEgress[0].ports[0].port=443",
            ]
        )
        extra = policy["spec"]["egress"][-1]
        assert extra["to"] == [{"ipBlock": {"cidr": "10.42.7.0/24"}}]
        assert extra["ports"] == [{"protocol": "TCP", "port": 443}]


def test_internet_ports_narrow_public_access_when_set() -> None:
    policy = _agent_policy(
        [SHARED, "agentExecution.networkPolicy.internetPorts[0].port=443"]
    )
    public = next(
        rule
        for rule in policy["spec"]["egress"]
        if any("ipBlock" in peer for peer in rule.get("to", []))
    )
    assert public["ports"] == [{"protocol": "TCP", "port": 443}]


def test_turning_off_api_egress_removes_only_that_rule() -> None:
    policy = _agent_policy(
        [SHARED, "agentExecution.networkPolicy.allowPreloopAPI=false"]
    )
    assert _control_plane_rule(policy) is None
    assert _ip_blocks(policy)


def test_node_cidrs_readmit_kubelet_probes_and_nothing_else() -> None:
    """Environment sidecars carry a TCP startupProbe the kubelet runs from the node."""
    policy = _agent_policy(
        [SHARED, "agentExecution.networkPolicy.nodeCidrs={10.0.0.0/24,10.0.1.0/24}"]
    )
    assert policy["spec"]["ingress"] == [
        {
            "from": [
                {"ipBlock": {"cidr": "10.0.0.0/24"}},
                {"ipBlock": {"cidr": "10.0.1.0/24"}},
            ]
        }
    ]


def test_disabling_the_policy_keeps_the_namespace_quota() -> None:
    """The quota is about resources, not the network; it must not ride along."""
    rendered = helm_template(
        POLICY_TEMPLATE,
        overrides=[SEPARATE, "agentExecution.networkPolicy.enabled=false"],
    )
    kinds = {doc["kind"] for doc in _docs(rendered)}
    assert "NetworkPolicy" not in kinds
    assert kinds == {"ResourceQuota", "LimitRange"}


def test_shared_namespace_does_not_render_namespace_wide_objects() -> None:
    """A quota in the release namespace would cap Preloop's own pods."""
    rendered = helm_template(POLICY_TEMPLATE, overrides=[SHARED])
    kinds = {doc["kind"] for doc in _docs(rendered)}
    assert kinds == {"NetworkPolicy"}


# --- Cilium variant ---------------------------------------------------------


def test_cilium_variant_replaces_the_agent_policy_but_not_the_namespace_deny() -> None:
    rendered = helm_template_all(overrides=[CILIUM, SEPARATE])
    docs = _docs(rendered)
    kinds = {(doc["kind"], doc["metadata"]["name"]) for doc in docs}
    assert ("CiliumNetworkPolicy", "preloop-agent-execution") in kinds
    assert ("NetworkPolicy", "preloop-agent-execution") not in kinds
    assert ("NetworkPolicy", "agent-execution-isolation") in kinds


def test_cilium_variant_lands_in_the_agent_namespace() -> None:
    assert _cilium_policy([SHARED])["metadata"]["namespace"] == "default"
    assert _cilium_policy([SEPARATE])["metadata"]["namespace"] == "agent-executions"


def test_cilium_variant_selects_agent_pods_and_admits_only_the_host_on_ingress() -> (
    None
):
    policy = _cilium_policy()
    assert policy["spec"]["endpointSelector"] == {
        "matchLabels": {"app": "agent-execution"}
    }
    assert policy["spec"]["ingress"] == [{"fromEntities": ["host"]}]
    assert "ingressDeny" not in policy["spec"]


def test_cilium_variant_can_deny_all_ingress_explicitly() -> None:
    policy = _cilium_policy(
        ["agentExecution.networkPolicy.cilium.allowHostIngress=false"]
    )
    assert policy["spec"]["ingress"] == [{}]
    assert policy["spec"]["ingressDeny"] == [{"fromEntities": ["all"]}]


def test_cilium_variant_reaches_the_world_and_the_host_without_cidrs() -> None:
    """ipBlock never matches a node on Cilium; the host entity is the hairpin."""
    policy = _cilium_policy()
    internet = next(rule for rule in policy["spec"]["egress"] if "toEntities" in rule)
    assert internet["toEntities"] == ["world", "host"]
    assert "toPorts" not in internet
    assert "ipBlock" not in yaml.safe_dump(policy)
    assert "toCIDRSet" not in yaml.safe_dump(policy["spec"]["egress"])


def test_cilium_variant_denies_the_metadata_address() -> None:
    policy = _cilium_policy()
    assert policy["spec"]["egressDeny"] == [{"toCIDR": [METADATA_ADDRESS]}]


def test_cilium_variant_allows_dns_and_control_plane_by_endpoint() -> None:
    policy = _cilium_policy()
    dns, control_plane = policy["spec"]["egress"][0], policy["spec"]["egress"][1]
    assert dns["toEndpoints"] == [
        {
            "matchLabels": {
                "k8s:io.kubernetes.pod.namespace": "kube-system",
                "k8s:k8s-app": "kube-dns",
            }
        }
    ]
    assert dns["toPorts"] == [
        {
            "ports": [
                {"port": "53", "protocol": "UDP"},
                {"port": "53", "protocol": "TCP"},
            ]
        }
    ]
    components = sorted(
        endpoint["matchLabels"]["k8s:app.kubernetes.io/component"]
        for endpoint in control_plane["toEndpoints"]
    )
    assert components == ["api", "gateway"]
    for endpoint in control_plane["toEndpoints"]:
        assert endpoint["matchLabels"]["k8s:io.kubernetes.pod.namespace"] == "default"
        assert endpoint["matchLabels"]["k8s:app.kubernetes.io/name"] == "preloop"
        assert "k8s:app.kubernetes.io/instance" in endpoint["matchLabels"]
    assert control_plane["toPorts"] == [
        {
            "ports": [
                {"port": "80", "protocol": "TCP"},
                {"port": "8000", "protocol": "TCP"},
            ]
        }
    ]


def test_cilium_variant_knobs(tmp_path) -> None:
    # Helm deep-merges maps from values files and --set alike, so moving DNS
    # to another namespace means nulling the default keys, not just adding
    # new ones. This is the idiom values.yaml documents; test that path.
    dns_values = tmp_path / "dns.yaml"
    dns_values.write_text(
        yaml.safe_dump(
            {
                "agentExecution": {
                    "networkPolicy": {
                        "dns": {
                            "namespaceSelectorLabels": {
                                "kubernetes.io/metadata.name": None,
                                "name": "openshift-dns",
                            },
                            "podSelectorLabels": {
                                "k8s-app": None,
                                "dns.operator.openshift.io/daemonset-dns": "default",
                            },
                        }
                    }
                }
            }
        )
    )
    rendered = helm_template(
        CILIUM_TEMPLATE,
        overrides=[
            CILIUM,
            "agentExecution.networkPolicy.cilium.internetEntities={world,host,remote-node}",
            "agentExecution.networkPolicy.internetPorts[0].port=443",
            "agentExecution.networkPolicy.cilium.extraEgress[0].toCIDR[0]=203.0.113.0/24",
        ],
        values_files=[str(dns_values)],
    )
    policy = next(
        doc for doc in _docs(rendered) if doc["kind"] == "CiliumNetworkPolicy"
    )
    egress = policy["spec"]["egress"]
    assert egress[0]["toEndpoints"] == [
        {
            "matchLabels": {
                "k8s:io.kubernetes.pod.namespace.labels.name": "openshift-dns",
                "k8s:dns.operator.openshift.io/daemonset-dns": "default",
            }
        }
    ]
    internet = next(rule for rule in egress if "toEntities" in rule)
    assert internet["toEntities"] == ["world", "host", "remote-node"]
    assert internet["toPorts"] == [{"ports": [{"port": "443", "protocol": "TCP"}]}]
    assert egress[-1] == {"toCIDR": ["203.0.113.0/24"]}


def test_cilium_variant_never_names_the_database_or_message_bus() -> None:
    text = yaml.safe_dump(_cilium_policy()["spec"])
    for needle in ("5432", "4222", "component: console", "component: database", "nats"):
        assert needle not in text


def _endpoint_selector_keys(policy: Dict) -> List[str]:
    """Every label key used inside a toEndpoints or fromEndpoints selector."""
    keys: List[str] = []
    for direction in ("ingress", "egress", "ingressDeny", "egressDeny"):
        for rule in policy["spec"].get(direction) or []:
            for field in ("toEndpoints", "fromEndpoints"):
                for selector in rule.get(field) or []:
                    keys.extend(selector.get("matchLabels") or {})
                    keys.extend(
                        req["key"] for req in selector.get("matchExpressions") or []
                    )
    return keys


def test_cilium_variant_prefixes_every_label_key_in_endpoint_selectors() -> None:
    """Cilium reads a bare key inside toEndpoints as any:, which also matches
    labels from other sources. k8s: names the pod label, while caller-supplied
    sources stay explicit. Neither layout may fall back to a bare key."""
    custom_dns = [
        "agentExecution.networkPolicy.dns.podSelectorLabels.k8s-app=null",
        "agentExecution.networkPolicy.dns.podSelectorLabels.app=coredns",
        "agentExecution.networkPolicy.dns.podSelectorLabels.any:tier=dns",
        "agentExecution.networkPolicy.dns.podSelectorLabels.container:app=dns",
    ]
    for overrides in ([], [SEPARATE], custom_dns):
        keys = _endpoint_selector_keys(_cilium_policy(overrides))
        assert keys, "no endpoint selector rendered"
        assert all(":" in key for key in keys), keys


def test_cilium_variant_keeps_a_caller_supplied_label_source() -> None:
    policy = _cilium_policy(
        ["agentExecution.networkPolicy.dns.podSelectorLabels.any:tier=dns"]
    )
    dns = policy["spec"]["egress"][0]["toEndpoints"][0]["matchLabels"]
    assert dns["any:tier"] == "dns"
    assert dns["k8s:k8s-app"] == "kube-dns"
    assert "k8s:any:tier" not in dns


# --- documentation that the templates depend on ----------------------------


def test_notes_and_readme_describe_the_legacy_fallback_and_cilium() -> None:
    notes = (CHART_DIR / "templates" / "NOTES.txt").read_text()
    assert "excludeCIDRs is deprecated" in notes
    assert "cilium.enabled=true" in notes
    readme = (CHART_DIR / "README.md").read_text()
    assert "agentExecution.networkPolicy.cilium.enabled" in readme
    assert "not hashed" in readme, "checksum qualifier for operator Secrets missing"

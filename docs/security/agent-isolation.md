# Agent pod isolation

An agent pod runs code a model wrote, on a repository the model can edit,
with a credential the platform minted for it. It is the least trusted thing
in the deployment. This page states what such a pod can reach, what the
chart's NetworkPolicy changes, and what is still open afterwards.

## What runs, and where

The executor creates one Job per flow execution
(`backend/preloop/agents/container.py`). Its pod carries the labels
`app=agent-execution`, `preloop.agent_type`, `preloop.flow_id`, and
`preloop.execution_id`. The namespace comes from
`AGENT_EXECUTION_NAMESPACE`: the chart points it at a dedicated namespace
when `agentExecution.namespace.create` is true, and at the release
namespace otherwise. The second case is the easy one to deploy and the
dangerous one to leave unguarded, because the database, NATS, the console,
and the other tenants' agent pods are then neighbours.

Nothing dials into an agent pod. Output leaves it two ways:

- the pod log stream, which the runner reads through the API server
  (`read_namespaced_pod_log`), carrying the result artifact, the evidence
  archive, and the workspace snapshot as base64 lines;
- direct uploads made by the pod itself, when checkpointing or evidence
  upload is enabled (`backend/preloop/services/checkpoint_runtime.py`),
  which are outbound HTTP calls to the public Preloop URL.

So ingress can be denied outright, and the policy does.

## Without a NetworkPolicy

A cluster with no policy gives every pod a flat network. From an agent pod
that means:

- `preloop-db-rw:5432`, with whatever credentials it can find. If the chart
  was installed with its default values, those credentials are also the
  defaults, and the connection string used to sit in the API pod spec,
  which any in-cluster reader could fetch.
- `preloop-nats:4222`, the event bus every worker reads from.
- the console and the API, on every port, not just HTTP.
- other agent pods, which belong to other flows and possibly other
  accounts.
- the cloud metadata service, if the node exposes one.

None of that is needed to run an agent.

## With the policy

`helm/preloop/templates/agent-networkpolicy.yaml` selects pods labelled
`app=agent-execution` and:

- denies all ingress;
- allows egress to kube-dns on 53;
- allows egress to the API and gateway pods on the HTTP ports. Both 80 and
  8000 are listed: a ClusterIP connection is translated to the pod IP and
  target port before policy is evaluated on most CNIs, so a rule naming
  only the service port silently drops the traffic;
- allows egress to `0.0.0.0/0` minus the cluster pod and service CIDRs
  (`agentExecution.networkPolicy.clusterCidrs`), which is what model
  providers, git remotes, and package registries need;
- allows nothing else in-cluster. The database, NATS, the console, and
  other agent pods fall off the list.

`agentExecution.networkPolicy.extraEgress` takes verbatim rules for the
endpoints a particular deployment needs (an in-cluster registry, a
node-local DNS cache on a link-local address, an internal artifact store).

A second, opt-in policy
(`agentExecution.networkPolicy.controlPlaneIngress`) says the same thing
from the API and gateway side: traffic from `app=agent-execution` pods is
accepted on the HTTP ports only, everything else is accepted as before. It
needs the cluster pod CIDRs, because an ingress rule that did not re-admit
node-sourced traffic would also cut off load balancer health checks and
host-network ingress controllers.

Policies only do something on a CNI that enforces them. Cilium, Calico, and
Antrea do. On a CNI that does not, these objects render and have no effect.

## The credential an agent carries

`create_flow_runtime_token`
(`backend/preloop/services/flow_runtime_token.py`) mints one API key per
execution, named `Flow Execution <execution id>`:

- scopes `mcp:read` and `mcp:write`;
- two hour expiry;
- `context_data` binding it to the flow, the execution, the runtime
  session, and the flow's allowed MCP servers and tools;
- owned by the account's primary active user, because MCP tools act on that
  account;
- revoked when the execution ends, by execution rather than by key id, so
  an execution handed between workers does not leave a live key behind
  (`revoke_flow_runtime_tokens`).

The key reaches the pod as `PRELOOP_API_TOKEN` (and inside `MCP_CONFIG_JSON`)
only when the flow allows MCP servers or tools.

The limits worth knowing:

- **Scopes are recorded, not enforced.** The generic API key path
  (`backend/preloop/api/auth/jwt.py`) authenticates the key and returns the
  owning user; it does not compare the requested route against the key's
  scopes, and the MCP HTTP layer says as much in
  `backend/preloop/services/mcp_http.py` ("we do not use scopes"). For two
  hours the token is as powerful as the user it belongs to.
- **The allow lists live in the token context, not in the token check.**
  `allowed_mcp_servers` and `allowed_mcp_tools` scope what the MCP layer
  offers the agent; they are not a second authorization boundary.

Reducing that blast radius is a backend change, not a chart change: enforce
the scopes on the key, and give the runtime principal its own role instead
of the primary user's.

## Residual risks

- **Cloud metadata service.** 169.254.169.254 is link-local, so it is not
  covered by the cluster CIDR carve-out and stays reachable unless the node
  pool blocks it (IMDSv2 hop limit 1, GKE metadata concealment, or an
  explicit `extraEgress` deny is not expressible in NetworkPolicy; use a
  CiliumNetworkPolicy or the node configuration).
- **Other namespaces.** The policy names the release namespace for the
  control plane and the internet for everything else. A workload in a third
  namespace with a routable ClusterIP is unreachable, but a workload
  reachable on a public address is not.
- **DNS exfiltration.** Egress to kube-dns on 53 is unrestricted in
  content. Data can leave in query names. Restricting that needs a DNS-aware
  policy (CiliumNetworkPolicy `toFQDNs`) or an egress proxy.
- **Egress to the whole internet.** The policy limits where an agent can go
  inside the cluster, not what it can post to a pastebin. Deployments that
  need that constraint should replace the `0.0.0.0/0` rule with an FQDN
  policy or route agents through a proxy (`extraEgress` plus the proxy env
  in `agentExecution`).
- **The API is still one hop away.** MCP and the model gateway are exactly
  what the agent is supposed to reach, so the credential above, not the
  network, is what limits it.
- **Shared namespace, shared service account.** Agent Jobs do not set
  `serviceAccountName`, so they run as the namespace default account, and
  the token is only left unmounted in isolated publication mode
  (`automount_service_account_token=False`). A policy does not stop a pod
  from using a token it holds, so the default account in the agent
  namespace must stay unprivileged. The carve-out does help here: the API
  server's ClusterIP lives in the service CIDR, so the policy removes the
  usual path to it, and the agent manager Role
  (`helm/preloop/templates/agent-rbac.yaml`) is bound to the Preloop
  service account, not to the agents.

## Related

- `docs/operations/database-credentials.md`: rotating the database
  credentials the old topology exposed.
- `docs/architecture/security.md`: platform-wide model.

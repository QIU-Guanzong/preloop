#!/usr/bin/env sh
# Helm chart render checks for the preloop chart, focused on the CNPG backup
# wiring. Run from the repo root. Requires helm 3 and network access for
# `helm dependency build` (nats subchart).
set -eu

CHART=./helm/preloop

echo "==> helm dependency build"
helm repo add nats https://nats-io.github.io/k8s/helm/charts >/dev/null 2>&1 || true
helm dependency build "$CHART" >/dev/null

echo "==> helm lint"
helm lint "$CHART"

fail() { echo "FAIL: $1" >&2; exit 1; }

echo "==> defaults: backup must be OFF"
out=$(helm template t "$CHART")
echo "$out" | grep -q "kind: ScheduledBackup" && fail "ScheduledBackup rendered with defaults"
echo "$out" | grep -q "barmanObjectStore" && fail "barmanObjectStore rendered with defaults"

echo "==> prod profile: backup + ScheduledBackup ON"
out=$(helm template t "$CHART" -f "$CHART/values-backup-prod.yaml")
echo "$out" | grep -q "kind: ScheduledBackup" || fail "ScheduledBackup missing (prod profile)"
echo "$out" | grep -q "barmanObjectStore" || fail "barmanObjectStore missing (prod profile)"
echo "$out" | grep -q 'retentionPolicy: "30d"' || fail "prod retention wrong"
echo "$out" | grep -q 'schedule: "0 0 2 \* \* \*"' || fail "prod schedule wrong"
echo "$out" | grep -q "backupOwnerReference: none" || fail "prod backupOwnerReference should be none"

echo "==> staging profile: backup + ScheduledBackup ON"
out=$(helm template t "$CHART" -f "$CHART/values-backup-staging.yaml")
echo "$out" | grep -q "kind: ScheduledBackup" || fail "ScheduledBackup missing (staging profile)"
echo "$out" | grep -q 'retentionPolicy: "7d"' || fail "staging retention wrong"
echo "$out" | grep -q 'schedule: "0 0 3 \* \* \*"' || fail "staging schedule wrong"
echo "$out" | grep -q "backupOwnerReference: none" || fail "staging backupOwnerReference should be none"

echo "==> backup.enabled without destinationPath must fail fast"
if helm template t "$CHART" --set database.cnpg.backup.enabled=true >/dev/null 2>&1; then
  fail "template rendered despite missing destinationPath"
fi

echo "==> scheduled.enabled=false suppresses ScheduledBackup only"
out=$(helm template t "$CHART" -f "$CHART/values-backup-prod.yaml" \
  --set database.cnpg.backup.scheduled.enabled=false)
echo "$out" | grep -q "kind: ScheduledBackup" && fail "ScheduledBackup rendered when scheduled.enabled=false"
echo "$out" | grep -q "barmanObjectStore" || fail "WAL archiving suppressed by scheduled.enabled=false"

echo "==> endpointURL / serverName render when set"
out=$(helm template t "$CHART" -f "$CHART/values-backup-prod.yaml" \
  --set database.cnpg.backup.endpointURL=https://minio.example.com \
  --set database.cnpg.backup.serverName=preloop-db-v2)
echo "$out" | grep -q "endpointURL: https://minio.example.com" || fail "endpointURL not rendered"
echo "$out" | grep -q "serverName: preloop-db-v2" || fail "serverName not rendered"

echo "==> service role, flow inflight, gateway memory request"
out=$(helm template t "$CHART")
echo "$out" | grep -A1 'name: PRELOOP_SERVICE_ROLE' | grep -q 'value: "gateway"' \
  || fail "PRELOOP_SERVICE_ROLE=gateway missing"
echo "$out" | grep -A1 'name: PRELOOP_SERVICE_ROLE' | grep -q 'value: "api"' \
  || fail "PRELOOP_SERVICE_ROLE=api missing"
echo "$out" | grep -A1 'name: FLOW_EXECUTION_MAX_INFLIGHT' | grep -q 'value: "10"' \
  || fail "FLOW_EXECUTION_MAX_INFLIGHT missing"
echo "$out" | grep -q 'memory: 768Mi' || fail "gateway memory request 768Mi missing"

echo "==> agent isolation: policy renders by default, in the release namespace"
np=$(helm template t "$CHART" --namespace preloop \
  --show-only templates/agent-networkpolicy.yaml | grep -v '^ *#' | grep -v '^$')
echo "$np" | grep -q "name: t-preloop-agent-execution" \
  || fail "agent NetworkPolicy missing with defaults"
echo "$np" | grep -q "namespace: preloop" \
  || fail "agent policy not placed in the release namespace"
echo "$np" | grep -A2 'podSelector:' | grep -q 'app: agent-execution' \
  || fail "agent policy does not select app=agent-execution pods"
echo "$np" | grep -q 'ingress: \[\]' || fail "agent policy does not deny ingress"
echo "$np" | grep -q 'port: 53' || fail "agent policy blocks DNS"
echo "$np" | grep -q 'app.kubernetes.io/component: api' \
  || fail "agent policy has no egress to the API"
echo "$np" | grep -q 'app.kubernetes.io/component: gateway' \
  || fail "agent policy has no egress to the gateway"
echo "$np" | grep -q 'port: 8000' \
  || fail "agent policy omits the container port (ClusterIP traffic is DNATed to it)"
echo "$np" | grep -q 'cidr: 0.0.0.0/0' || fail "agent policy has no internet egress"
echo "$np" | grep -q '10.0.0.0/8' || fail "cluster CIDRs not carved out of the internet rule"
# Negative assertions: the reason this policy exists.
echo "$np" | grep -q '5432' && fail "agent policy allows the database port"
echo "$np" | grep -q '4222' && fail "agent policy allows the NATS port"
echo "$np" | grep -q 'component: console' && fail "agent policy allows the console"

echo "==> agent isolation: dedicated namespace keeps the namespace-wide deny"
out=$(helm template t "$CHART" --set agentExecution.namespace.create=true)
echo "$out" | grep -q "name: agent-execution-isolation" \
  || fail "namespace default-deny missing when namespace.create=true"
echo "$out" | grep -q "namespace: agent-executions" \
  || fail "agent policy not placed in the agent namespace"

echo "==> agent isolation: can be turned off"
out=$(helm template t "$CHART" --set agentExecution.networkPolicy.enabled=false)
echo "$out" | grep -q "kind: NetworkPolicy" \
  && fail "NetworkPolicy rendered while networkPolicy.enabled=false"

echo "==> agent isolation: internet egress without cluster CIDRs must fail fast"
if helm template t "$CHART" --set 'agentExecution.networkPolicy.clusterCidrs=null' \
  --set 'agentExecution.networkPolicy.excludeCIDRs=null' >/dev/null 2>&1; then
  fail "template rendered an internet egress rule with no cluster CIDRs"
fi

echo "==> control plane ingress policy: opt in, needs pod CIDRs"
if helm template t "$CHART" \
  --set agentExecution.networkPolicy.controlPlaneIngress.enabled=true >/dev/null 2>&1; then
  fail "control plane policy rendered without podCidrs"
fi
out=$(helm template t "$CHART" \
  --set agentExecution.networkPolicy.controlPlaneIngress.enabled=true \
  --set 'agentExecution.networkPolicy.controlPlaneIngress.podCidrs={10.244.0.0/16}')
echo "$out" | grep -q "name: t-preloop-control-plane-ingress" \
  || fail "control plane policy missing when enabled"
echo "$out" | grep -q "10.244.0.0/16" || fail "pod CIDR not carved out of the ipBlock"

echo "==> credentials: no literal DATABASE_URL or SMTP_PASSWORD in any pod spec"
out=$(helm template t "$CHART" --set config.smtp.host=smtp.example.com \
  --set config.smtp.password=not-a-real-password)
echo "$out" | grep -A1 'name: DATABASE_URL' | grep -q 'value: postgresql' \
  && fail "DATABASE_URL still rendered as a literal env value"
hits=$(echo "$out" | grep -c 'not-a-real-password' || true)
[ "$hits" = "1" ] \
  || fail "SMTP password should appear once (in the Secret), saw $hits occurrences"
echo "$out" |  grep -A4 'name: DATABASE_URL' | grep -q 'key: "database-url"' \
  || fail "DATABASE_URL secretKeyRef missing"
echo "$out" | grep -q 'checksum/credentials:' \
  || fail "credentials checksum annotation missing (pods would not roll)"

echo "==> credentials: operator Secret wins and the chart Secret is dropped"
out=$(helm template t "$CHART" --set database.urlFromSecret.name=my-db-secret)
echo "$out" |  grep -A4 'name: DATABASE_URL' | grep -q 'name: "my-db-secret"' \
  || fail "database.urlFromSecret not honoured"
echo "$out" | grep -q "t-preloop-credentials" \
  && fail "chart credentials Secret rendered even though urlFromSecret is set"

echo "==> superuser access can be disabled"
out=$(helm template t "$CHART" --set database.cnpg.enableSuperuserAccess=false)
echo "$out" | grep -q "enableSuperuserAccess: false" \
  || fail "enableSuperuserAccess knob not wired"
echo "$out" | grep -q "t-preloop-db-superuser" \
  && fail "superuser Secret rendered while superuser access is disabled"

echo "All helm render checks passed."

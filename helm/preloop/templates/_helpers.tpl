{{/*
Expand the name of the chart.
*/}}
{{- define "preloop.name" -}}
{{- default .Chart.Name .Values.nameOverride | replace "." "-" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "preloop.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | replace "." "-" | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | replace "." "-" | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | replace "." "-" | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "preloop.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "preloop.labels" -}}
helm.sh/chart: {{ include "preloop.chart" . }}
{{ include "preloop.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "preloop.selectorLabels" -}}
app.kubernetes.io/name: {{ include "preloop.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Label key for a Cilium endpoint selector inside an ingress or egress rule.
Cilium reads a bare key there as any:, which matches the label from any
source; k8s: names the pod label and is the form its documentation uses.
A key that already carries a source (contains a colon) is left alone.
*/}}
{{- define "preloop.ciliumLabelKey" -}}
{{- if contains ":" . }}{{ . }}{{ else }}k8s:{{ . }}{{ end -}}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "preloop.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "preloop.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Create the database connection URL
*/}}
{{- define "preloop.databaseUrl" -}}
{{- if .Values.database.enabled -}}
{{- if .Values.database.external -}}
{{- $ssl := .Values.database.externalDatabase.sslMode -}}
postgresql://{{ .Values.database.externalDatabase.user }}:{{ .Values.database.externalDatabase.password }}@{{ .Values.database.externalDatabase.host }}:{{ .Values.database.externalDatabase.port }}/{{ .Values.database.externalDatabase.database }}{{- if $ssl }}?sslmode={{ $ssl }}{{- end }}
{{- else -}}
{{- if .Values.database.cnpg.name -}}
postgresql://{{ .Values.database.cnpg.auth.username | default "postgres" }}:{{ .Values.database.cnpg.auth.password | default "" }}@{{ .Values.database.cnpg.name }}-rw:5432/{{ .Values.database.cnpg.auth.database }}
{{- else -}}
postgresql://{{ .Values.database.cnpg.auth.username | default "postgres" }}:{{ .Values.database.cnpg.auth.password | default "" }}@{{ include "preloop.fullname" . }}-db-rw:5432/{{ .Values.database.cnpg.auth.database }}
{{- end -}}
{{- end -}}
{{- else -}}
{{ .Values.environment.databaseUrl }}
{{- end -}}
{{- end }}

{{/*
Name of the application Secret. When existingSecret is set, pods read
jwt-secret (and other keys) from that Secret instead of the chart-generated one.
*/}}
{{- define "preloop.secretName" -}}
{{- if .Values.existingSecret -}}
{{- .Values.existingSecret -}}
{{- else -}}
{{- include "preloop.fullname" . -}}
{{- end -}}
{{- end }}

{{/*
Name of the Secret holding credentials the chart derives from values
(DATABASE_URL, SMTP password). Separate from preloop.secretName so that an
operator-supplied existingSecret does not have to carry these keys.
*/}}
{{- define "preloop.credentialsSecretName" -}}
{{- printf "%s-credentials" (include "preloop.fullname" .) -}}
{{- end }}

{{/*
DATABASE_URL env entry. Always a secretKeyRef: either the operator's own
Secret (database.urlFromSecret, which also keeps the URL out of values) or
the chart-managed credentials Secret. Never a literal in the pod spec.
*/}}
{{- define "preloop.databaseUrlEnv" -}}
- name: DATABASE_URL
  valueFrom:
    secretKeyRef:
{{- if and .Values.database.urlFromSecret .Values.database.urlFromSecret.name }}
      name: {{ .Values.database.urlFromSecret.name | quote }}
      key: {{ default "database-url" .Values.database.urlFromSecret.key | quote }}
{{- else }}
      name: {{ include "preloop.credentialsSecretName" . | quote }}
      key: "database-url"
{{- end }}
{{- end }}

{{/*
SMTP_PASSWORD env entry, same rule as DATABASE_URL. Call with the root
context ($) so it also works inside a range.
*/}}
{{- define "preloop.smtpPasswordEnv" -}}
{{- $smtpSecret := .Values.config.smtp.passwordSecret | default dict -}}
- name: SMTP_PASSWORD
  valueFrom:
    secretKeyRef:
{{- if $smtpSecret.name }}
      name: {{ $smtpSecret.name | quote }}
      key: {{ default "smtp-password" $smtpSecret.key | quote }}
{{- else }}
      name: {{ include "preloop.credentialsSecretName" . | quote }}
      key: "smtp-password"
{{- end }}
{{- end }}

{{/*
Annotation that rolls pods when a chart-managed credential changes. Env
literals used to do this implicitly; a Secret reference does not, so the
checksum has to be carried on the pod template. Call with the root context.
*/}}
{{- define "preloop.credentialsChecksum" -}}
checksum/credentials: {{ include (print $.Template.BasePath "/secret-credentials.yaml") . | sha256sum }}
{{- end }}

{{/*
Operator-supplied extra env vars (API, gateway, workers, jobs).
*/}}
{{- define "preloop.extraEnv" -}}
{{- with .Values.extraEnv -}}
{{- toYaml . -}}
{{- end -}}
{{- end }}

{{/*
Shared OTLP env for API and gateway (one values schema).
*/}}
{{- define "preloop.otlpEnv" -}}
{{- if .Values.otlp.enabled }}
- name: OTLP_ENABLED
  value: "true"
- name: OTLP_ENDPOINT
  value: {{ .Values.otlp.endpoint | quote }}
- name: OTLP_PROTOCOL
  value: {{ .Values.otlp.protocol | quote }}
- name: OTLP_SERVICE_NAME
  value: {{ .Values.otlp.resource.serviceName | quote }}
{{- if .Values.otlp.resource.serviceNamespace }}
- name: OTLP_SERVICE_NAMESPACE
  value: {{ .Values.otlp.resource.serviceNamespace | quote }}
{{- end }}
{{- if .Values.otlp.resource.deploymentEnvironment }}
- name: OTLP_DEPLOYMENT_ENVIRONMENT
  value: {{ .Values.otlp.resource.deploymentEnvironment | quote }}
{{- end }}
- name: OTLP_SAMPLER_RATIO
  value: {{ .Values.otlp.samplerRatio | quote }}
{{- $headersSecretName := .Values.otlp.headersSecret.name | default "" }}
{{- $headersSecretKey := .Values.otlp.headersSecret.key | default "otlp-headers" }}
{{- if $headersSecretName }}
- name: OTLP_HEADERS
  valueFrom:
    secretKeyRef:
      name: {{ $headersSecretName | quote }}
      key: {{ $headersSecretKey | quote }}
{{- else if .Values.otlp.headers }}
- name: OTLP_HEADERS
  valueFrom:
    secretKeyRef:
      name: {{ include "preloop.fullname" . }}
      key: otlp-headers
{{- end }}
{{- end }}
{{- end }}

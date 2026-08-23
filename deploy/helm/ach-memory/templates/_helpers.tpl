{{- define "ach-memory.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "ach-memory.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "ach-memory.labels" -}}
app.kubernetes.io/name: {{ include "ach-memory.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "ach-memory.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ach-memory.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
MEMORY_MASTER_KEY_HASH must come from a Secret, never a plain values.yaml
string -- it is the credential that reaches every bank in the tenant. This
resolves the Secret name to reference; templates/secret-master-key.yaml is
what fails rendering if neither masterKeySecret.name nor .value was given.
*/}}
{{- define "ach-memory.masterKeySecretName" -}}
{{- if .Values.masterKeySecret.name -}}
{{ .Values.masterKeySecret.name }}
{{- else -}}
{{ include "ach-memory.fullname" . }}-master-key
{{- end -}}
{{- end -}}

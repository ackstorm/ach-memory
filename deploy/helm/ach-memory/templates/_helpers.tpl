{{- define "ach-memory.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "ach-memory.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "ach-memory.databaseUrlEnv" -}}
- name: MEMORY_DATABASE_URL
{{- if .Values.config.databaseUrlSecret.name }}
  valueFrom:
    secretKeyRef:
      name: {{ .Values.config.databaseUrlSecret.name }}
      key: {{ .Values.config.databaseUrlSecret.key }}
{{- else }}
  value: {{ required "config.databaseUrl or config.databaseUrlSecret.name is required (MEMORY_DATABASE_URL)" .Values.config.databaseUrl | quote }}
{{- end }}
{{- end }}

{{- define "bathurstbot.labels" -}}
app.kubernetes.io/part-of: bathurstbot
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end }}

{{/* The pod and container security the dmz zone's Pod Security (restricted) requires. */}}
{{- define "bathurstbot.podSecurity" -}}
runAsNonRoot: true
runAsUser: {{ .uid }}
runAsGroup: {{ .uid }}
fsGroup: {{ .uid }}
fsGroupChangePolicy: OnRootMismatch
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "bathurstbot.containerSecurity" -}}
allowPrivilegeEscalation: false
capabilities:
  drop: [ALL]
{{- end }}

{{- define "bathurstbot.image" -}}
{{- printf "%s:%s" .repository (required (printf "%s: tag is required (CI writes it)" .repository) .tag) | quote -}}
{{- end }}

{{- define "bathurstbot.keep" -}}
{{- if . }}
annotations:
  argocd.argoproj.io/sync-options: Prune=false,Delete=false
  helm.sh/resource-policy: keep
{{- end }}
{{- end }}

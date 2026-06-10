{{/* ServiceAccounts (e.g. Workload Identity KSAs) */}}

{{- define "pipeline.serviceaccounts" }}
{{- range . }}
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ .name | quote }}
  namespace: {{ .namespace | default "default" | quote }}
  {{- with .annotations }}
  annotations:
    {{- range $key, $val := . }}
    {{ $key }}: {{ $val | quote }}
    {{- end }}
  {{- end }}
---
{{- end }}
{{- end }}

{{/* ConfigMap from an env list */}}

{{- define "pipeline.configmap-from-env" }}
{{- range . }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .name }}
  namespace: {{ .namespace | default "default" | quote }}
data:
  {{- range $key, $val := .vars }}
  {{ $key }}: {{ $val | quote }}
  {{- end }}
---
{{- end}}
{{- end }}

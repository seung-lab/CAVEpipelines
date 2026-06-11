{{/* Opaque Secrets from {name, namespace, data: {filename: base64}} */}}

{{- define "pipeline.secrets" }}
{{- range . }}
apiVersion: v1
kind: Secret
metadata:
  name: {{ .name }}
  namespace: {{ .namespace | default "default" | quote }}
type: Opaque
data:
  {{- range $key, $val := .data }}
  {{ $key }}: {{ $val | quote }}
  {{- end }}
---
{{- end }}
{{- end }}

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


{{/* ConfigMap from YAML values */}}

{{- define "pipeline.configmap-from-yaml" }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .name }}
  namespace: {{ .namespace | default "default" | quote }}
data:
  {{- range .files }}
  {{ .name }}: |-
    {{- toYaml .content | nindent 4 }}
  {{- end }}
---
{{- end }}


{{/* ConfigMap from file strings */}}

{{- define "pipeline.configmap-from-file-strings" }}
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .name }}
  namespace: {{ .namespace | default "default" | quote }}
data:
  {{- range $key, $val := .files }}
  {{ $key }}: |-
{{ $val | indent 4 }}
  {{- end }}
---
{{- end }}

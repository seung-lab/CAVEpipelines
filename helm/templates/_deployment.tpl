{{/* Kubernetes Deployment */}}

{{- define "pipeline.deployment" }}
{{- if .enabled }}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .name | quote }}
  namespace: {{ .namespace | default "default" | quote }}
spec:
{{- if not .hpa.enabled | default false }}
  replicas: {{ .replicaCount }}
{{- end }}
  selector:
    matchLabels:
      app: {{ .name | quote }}
  template:
    metadata:
      annotations:
        {{- if .helmRollOnUpgrade }}
        rollme: {{ randAlphaNum 5 | quote }}
        {{- end }}
        {{- range $key, $val := .annotations }}
        {{ $key }}: {{ $val | quote }}
        {{- end }}
      labels:
        app: {{ .name | quote }}
        {{- range $key, $val := .labels }}
        {{ $key }}: {{ $val | quote }}
        {{- end }}
    spec:
      serviceAccountName: {{ .serviceAccountName | default "default" | quote }}
      hostNetwork: {{ .hostNetwork | default false }}
      affinity:
        {{- toYaml .affinity | nindent 8 }}
      volumes:
        {{- toYaml .volumes | nindent 8 }}
      containers:
      {{- range .containers }}
      {{- template "pipeline.container" . }}
      {{- end }}
      imagePullSecrets:
        {{- toYaml .imagePullSecrets | nindent 8 }}
      nodeSelector:
        {{- toYaml .nodeSelector | nindent 8 }}
      {{- with .tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
---
{{- end }}
{{- end }}

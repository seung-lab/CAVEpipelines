{{/* Create kubernetes hpa object */}}

{{- define "common.hpa" }}
{{- if and .enabled .hpa.enabled }}
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: {{ .name | quote }}
  namespace: {{ .namespace | default "default" | quote }}
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: {{ .name | quote }}
  minReplicas: {{ .hpa.minReplicas }}
  maxReplicas: {{ .hpa.maxReplicas | default .hpa.minReplicas }}
  metrics:
  {{- if .hpa.targetCPU }}
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: {{ .hpa.targetCPU }}
  {{- end }}
  {{- if .hpa.targetMem }}
    - type: Resource
      resource:
        name: memory
        target:
          type: Utilization
          averageUtilization: {{ .hpa.targetMem }}
  {{- end }}
---
{{- end }}
{{- end }}

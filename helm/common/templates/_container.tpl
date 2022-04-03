{{/* container within pod/deployment*/}}

{{- define "common.containers" }}
containers:
- name: {{ .name | quote }}
  image: >-
    {{ required "repo" .image.repository }}:{{ required "tag" .image.tag }}
  imagePullPolicy: {{ .image.pullPolicy | default "Always" }}
  ports:
    {{- toYaml .ports | nindent 4 }}
  resources:
  {{- if .resources }}
    {{- toYaml .resources | nindent 4 }}
  {{- end }}
  envFrom:
  {{- if .envFrom }}
    {{- toYaml .envFrom | nindent 4 }}
  {{- end }}
  {{- range .env }}
  - configMapRef:
      name: {{ .name }}
  {{- end }}
  volumeMounts:
  {{- if .volumeMounts }}
    {{- toYaml .volumeMounts | nindent 4 }}
  {{- end }}
  {{- if .command }}
  command:
    {{- toYaml .command | nindent 4 }}
  {{- else }}
  command: [bash, -c, "trap : TERM INT; sleep infinity & wait"]
  {{- end }}
---
{{- end }}

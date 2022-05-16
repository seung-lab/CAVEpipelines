{{/* container within pod/deployment*/}}

{{- define "common.container" }}
      - name: {{ .name | quote }}
        image: >-
          {{ required "repo" .image.repository }}:{{ required "tag" .image.tag }}
        imagePullPolicy: {{ .image.pullPolicy | default "Always" }}
        ports:
          {{- toYaml .ports | nindent 10 }}
        resources:
        {{- if .resources }}
          {{- toYaml .resources | nindent 10 }}
        {{- end }}
        envFrom:
        {{- if .envFrom }}
          {{- toYaml .envFrom | nindent 10 }}
        {{- end }}
        {{- range .env }}
        - configMapRef:
            name: {{ .name }}
        {{- end }}
        volumeMounts:
        {{- if .volumeMounts }}
          {{- toYaml .volumeMounts | nindent 8 }}
        {{- end }}
        {{- if .command }}
        command:
          {{- toYaml .command | nindent 8 }}
        {{- else }}
        command: [bash, -c, "trap : TERM INT; sleep infinity & wait"]
        {{- end }}
{{- end }}

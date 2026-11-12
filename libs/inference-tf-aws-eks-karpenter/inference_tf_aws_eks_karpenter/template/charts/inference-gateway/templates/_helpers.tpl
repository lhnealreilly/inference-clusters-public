{{- /* Placement shared by Envoy, EPP and BBR pods (the tainted system node group). */ -}}
{{- define "inference-gateway.placement" -}}
{{- with .Values.podDefaults.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.podDefaults.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}

{{- define "inference-gateway.validate" -}}
{{- if not .Values.schedulers }}
{{- fail "inference-gateway: at least one scheduler is required" }}
{{- end }}
{{- $models := dict }}
{{- $names := dict }}
{{- range .Values.schedulers }}
{{- if hasKey $models .modelName }}
{{- fail (printf "inference-gateway: duplicate scheduler modelName %q (must be unique)" .modelName) }}
{{- end }}
{{- if hasKey $names .name }}
{{- fail (printf "inference-gateway: duplicate scheduler name %q (must be unique)" .name) }}
{{- end }}
{{- $_ := set $models .modelName true }}
{{- $_ := set $names .name true }}
{{- end }}
{{- end -}}

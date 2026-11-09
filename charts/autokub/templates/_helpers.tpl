{{- define "autokub.labels" -}}
app.kubernetes.io/name: autokub
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "autokub.image" -}}
{{ printf "%s/autokub-%s:%s" .root.Values.image.registry .name (.root.Values.image.tag | default .root.Chart.AppVersion) | quote }}
{{- end }}

{{- define "autokub.validate" -}}
{{- if not .Values.namespaces }}{{ fail "namespaces es obligatorio: indica al menos un namespace con sus targets" }}{{ end }}
{{- if not (has .Values.mode (list "propose" "execute")) }}{{ fail "mode debe ser propose o execute" }}{{ end }}
{{- if not (has .Values.proposer (list "llm" "rules")) }}{{ fail "proposer debe ser llm o rules" }}{{ end }}
{{- if and (eq .Values.model.provider "openai") (not .Values.model.base_url) }}{{ fail "model.base_url es obligatorio con provider: openai" }}{{ end }}
{{- $dns := "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" }}
{{- range $namespace, $settings := .Values.namespaces }}
{{- if not (regexMatch $dns $namespace) }}{{ fail (printf "namespace no válido: %q" $namespace) }}{{ end }}
{{- if not (kindIs "slice" $settings.targets) }}{{ fail (printf "namespaces.%s.targets debe ser una lista" $namespace) }}{{ end }}
{{- if not $settings.targets }}{{ fail (printf "namespaces.%s.targets no puede estar vacío" $namespace) }}{{ end }}
{{- range $settings.targets }}
{{- if not (and (kindIs "string" .) (regexMatch $dns .)) }}{{ fail (printf "target no válido en %s: %v" $namespace .) }}{{ end }}
{{- end }}
{{- end }}
{{- end }}

{{- define "autokub.podSecurity" -}}
securityContext:
  runAsNonRoot: true
  runAsUser: 65532
  runAsGroup: 65532
  seccompProfile:
    type: RuntimeDefault
{{- end }}

{{- define "autokub.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: [ALL]
{{- end }}

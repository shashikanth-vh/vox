{{/*
Central production-credential validation.

"prism.assertSecret" fails the render when a credential is still a known placeholder AND no
external Secret is configured for it. It is a no-op when:
  * an existingSecret reference is supplied (the inline value is unused — the auditor's
    usability fix: a valid external Secret must never be rejected for a stale inline value), or
  * the value is a real (non-placeholder) secret.

Placeholders recognised: "REPLACE" appearing ANYWHERE in the value — not just as a prefix —
so an EMBEDDED placeholder inside a composite string is caught too (e.g.
"svc_atlas:REPLACE-svc-atlas-key,svc_vox:REPLACE-svc-vox-key" starts with "svc_atlas:" but is
still all placeholders). Also the dev default "change-me-in-prod". Paths are read with `dig` so
a missing key is treated as empty (never a template error) — a valid deployment with real
secrets always renders.

Usage:
  {{- include "prism.assertSecret" (dict "name" "gateway.gatewaySecret" "value" $v "existingSecret" $es) }}
*/}}
{{- define "prism.assertSecret" -}}
{{- $v := .value | default "" -}}
{{- $es := .existingSecret | default "" -}}
{{- if eq $es "" -}}
{{- if or (contains "REPLACE" $v) (contains "change-me-in-prod" $v) -}}
{{- fail (printf "Production credential '%s' still contains a placeholder (%q). Supply real values (--set / your secret manager) or an existingSecret reference before deploying — composite keys must have EVERY entry replaced." .name $v) -}}
{{- end -}}
{{- end -}}
{{- end -}}

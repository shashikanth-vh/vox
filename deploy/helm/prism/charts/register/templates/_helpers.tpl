{{- define "register.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "register.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "register.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "register.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "register.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: prism
{{- end -}}

{{- define "register.selectorLabels" -}}
app.kubernetes.io/name: {{ include "register.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "register.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "register.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "register.secretName" -}}
{{- printf "%s-secret" (include "register.fullname" .) -}}
{{- end -}}

{{/* DB connection — the Register connects to the shared PRISM PostgreSQL (or a managed
     host); it never owns a database of its own. */}}
{{- define "register.dbHost" -}}{{ .Values.database.host }}{{- end -}}
{{- define "register.dbPort" -}}{{ .Values.database.port }}{{- end -}}
{{- define "register.dbName" -}}{{ .Values.database.name }}{{- end -}}
{{- define "register.dbUser" -}}{{ .Values.database.user }}{{- end -}}

{{/* Password value rendered into the chart-managed Secret (when not using existingSecret). */}}
{{- define "register.dbPasswordValue" -}}{{ .Values.database.password }}{{- end -}}

{{/* initContainer that blocks until PostgreSQL accepts connections — makes migration
     and app pods robust to the shared DB starting up alongside the Register. */}}
{{- define "register.waitForDb" -}}
- name: wait-for-db
  image: {{ .Values.waitForDbImage | quote }}
  imagePullPolicy: {{ .Values.image.pullPolicy }}
  securityContext:
    {{- toYaml .Values.securityContext | nindent 4 }}
  command:
    - sh
    - -c
    - |
      until pg_isready -h {{ include "register.dbHost" . }} -p {{ include "register.dbPort" . }} -U {{ include "register.dbUser" . }}; do
        echo "waiting for database {{ include "register.dbHost" . }}:{{ include "register.dbPort" . }}...";
        sleep 2;
      done
{{- end -}}

{{/* Common env vars shared by the Deployment and the migration Job. */}}
{{- define "register.envVars" -}}
- name: REGISTER_ENVIRONMENT
  value: {{ .Values.config.environment | quote }}
- name: REGISTER_LOG_LEVEL
  value: {{ .Values.config.logLevel | quote }}
- name: REGISTER_LOG_JSON
  value: {{ .Values.config.logJson | quote }}
- name: REGISTER_DEFAULT_TENANT_CODE
  value: {{ .Values.config.defaultTenantCode | quote }}
- name: REGISTER_REQUIRE_API_KEY
  value: {{ .Values.config.requireApiKey | quote }}
- name: REGISTER_ENFORCE_RLS
  value: {{ .Values.config.enforceRls | quote }}
- name: REGISTER_WEB_CONCURRENCY
  value: {{ .Values.config.webConcurrency | quote }}
- name: REGISTER_WORKER_TIMEOUT
  value: {{ .Values.config.workerTimeout | quote }}
- name: REGISTER_DB_POOL_SIZE
  value: {{ .Values.config.pool.size | quote }}
- name: REGISTER_DB_MAX_OVERFLOW
  value: {{ .Values.config.pool.maxOverflow | quote }}
- name: REGISTER_DB_STATEMENT_TIMEOUT_MS
  value: {{ .Values.config.pool.statementTimeoutMs | quote }}
- name: REGISTER_DB_LOCK_TIMEOUT_MS
  value: {{ .Values.config.pool.lockTimeoutMs | quote }}
- name: REGISTER_DB_IDLE_IN_TXN_TIMEOUT_MS
  value: {{ .Values.config.pool.idleInTxnTimeoutMs | quote }}
- name: REGISTER_DEFAULT_PAGE_SIZE
  value: {{ .Values.config.page.default | quote }}
- name: REGISTER_MAX_PAGE_SIZE
  value: {{ .Values.config.page.max | quote }}
# DB connection — the shared PRISM PostgreSQL (or a managed host), via database.*
- name: REGISTER_DB_HOST
  value: {{ include "register.dbHost" . | quote }}
- name: REGISTER_DB_PORT
  value: {{ include "register.dbPort" . | quote }}
- name: REGISTER_DB_NAME
  value: {{ include "register.dbName" . | quote }}
- name: REGISTER_DB_USER
  value: {{ include "register.dbUser" . | quote }}
- name: REGISTER_DB_PASSWORD
  valueFrom:
    secretKeyRef:
      {{- if .Values.database.existingSecret }}
      name: {{ .Values.database.existingSecret }}
      key: {{ .Values.database.existingSecretPasswordKey }}
      {{- else }}
      name: {{ include "register.secretName" . }}
      key: db-password
      {{- end }}
- name: REGISTER_API_KEYS
  valueFrom:
    secretKeyRef:
      {{- if .Values.apiKeys.existingSecret }}
      name: {{ .Values.apiKeys.existingSecret }}
      key: {{ .Values.apiKeys.existingSecretKey }}
      {{- else }}
      name: {{ include "register.secretName" . }}
      key: api-keys
      {{- end }}
- name: REGISTER_GATEWAY_SHARED_SECRET
  value: {{ .Values.gatewaySharedSecret | quote }}
{{- if .Values.internalSigningSecret }}
# Verify the gateway's SIGNED internal context and enforce the live grant it carries.
# Must equal the gateway's signing secret. Secret-backed, never plaintext env.
- name: REGISTER_INTERNAL_SIGNING_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ include "register.secretName" . }}
      key: internal-signing-secret
{{- end }}
{{- if .Values.adminApiKeys }}
# Separate admin credential for tenant administration (X-Admin-Key), Secret-backed.
- name: REGISTER_ADMIN_API_KEYS
  valueFrom:
    secretKeyRef:
      name: {{ include "register.secretName" . }}
      key: admin-api-keys
{{- end }}
# RBAC-mandatory mode: gated operations (delete/restore/import/audit/line creates)
# refuse machine callers without a user context. On by default in the umbrella.
- name: REGISTER_ENFORCE_RBAC
  value: {{ .Values.enforceRbac | quote }}
# Object storage (document bytes) — only wired when the backend is "s3".
- name: REGISTER_STORAGE_BACKEND
  value: {{ .Values.storage.backend | quote }}
{{- if eq .Values.storage.backend "s3" }}
{{- with .Values.storage.s3 }}
- name: REGISTER_S3_BUCKET
  value: {{ .bucket | quote }}
- name: REGISTER_S3_ENDPOINT_URL
  value: {{ .endpointUrl | quote }}
- name: REGISTER_S3_PUBLIC_ENDPOINT_URL
  value: {{ .publicEndpointUrl | quote }}
- name: REGISTER_S3_REGION
  value: {{ .region | quote }}
- name: REGISTER_S3_USE_SSL
  value: {{ .useSsl | quote }}
- name: REGISTER_S3_PATH_STYLE
  value: {{ .pathStyle | quote }}
- name: REGISTER_S3_PRESIGN_EXPIRY_SECONDS
  value: {{ .presignExpirySeconds | quote }}
- name: REGISTER_S3_AUTO_CREATE_BUCKET
  value: {{ .autoCreateBucket | quote }}
- name: REGISTER_S3_STREAM_THROUGH_API
  value: {{ .streamThroughApi | quote }}
- name: REGISTER_S3_ACCESS_KEY_ID
  value: {{ .accessKeyId | quote }}
- name: REGISTER_S3_SECRET_ACCESS_KEY
  valueFrom:
    secretKeyRef:
      {{- if .existingSecret }}
      name: {{ .existingSecret }}
      key: {{ .existingSecretKey }}
      {{- else }}
      name: {{ include "register.secretName" $ }}
      key: s3-secret-access-key
      {{- end }}
{{- end }}
{{- end }}
{{- end -}}

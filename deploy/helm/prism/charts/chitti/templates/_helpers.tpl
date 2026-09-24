{{- define "chitti.name" -}}
{{- default (printf "%s-chitti" .Release.Name) .Values.fullnameOverride | trunc 40 | trimSuffix "-" -}}
{{- end -}}
{{- define "chitti.artifact" -}}
{{- $id := required "chitti.artifactId is required (immutable image/model/ontology identifier)" .Values.artifactId -}}
{{- if not (regexMatch "^[a-z0-9][a-z0-9-]{0,15}$" $id) -}}
{{- fail "chitti.artifactId must be 1-16 lowercase letters, digits or hyphens" -}}
{{- end -}}
{{- $id -}}
{{- end -}}
{{- define "chitti.security" -}}
runAsNonRoot: true
runAsUser: 1000
runAsGroup: 1000
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: [ALL]
{{- end -}}
{{- define "chitti.retrievalEnv" -}}
- name: HOME
  value: /tmp
- name: CHITTI_MODEL_CACHE_DIR
  value: /models
- name: CHITTI_QDRANT_URL
  value: {{ printf "http://%s-qdrant:6333" (include "chitti.name" .) | quote }}
- name: CHITTI_QDRANT_COLLECTION_PREFIX
  value: {{ printf "chitti_%s" (include "chitti.artifact" . | replace "-" "_") | quote }}
- name: CHITTI_DENSE_MODEL
  value: BAAI/bge-small-en-v1.5
- name: CHITTI_DENSE_MODEL_REVISION
  value: 52398278842ec682c6f32300af41344b1c0b0bb2
- name: CHITTI_DENSE_DIMENSIONS
  value: "384"
- name: CHITTI_SPARSE_MODEL
  value: Qdrant/bm25
- name: CHITTI_SPARSE_MODEL_REVISION
  value: 22b8d2af71a76161e18dd432d2cee0eefa66e412
- name: CHITTI_RERANK_MODEL
  value: Xenova/ms-marco-MiniLM-L-6-v2
- name: CHITTI_RERANK_MODEL_REVISION
  value: a09144355adeed5f58c8ed011d209bf8ee5a1fec
{{- end -}}
{{- define "chitti.volumes" -}}
- name: models
  persistentVolumeClaim:
    claimName: {{ include "chitti.name" . }}-models-{{ include "chitti.artifact" . }}
- name: tmp
  emptyDir:
    sizeLimit: 256Mi
{{- end -}}

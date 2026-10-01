#!/usr/bin/env bash
# Export the approved VOCX conversations — transcripts, approved reports and
# the reviewer edit trail — as one JSON Lines file for the learning tool
# (services/vocx/evals/vox_learn.py). Read-only on the database.
#
#   sudo ./prism/deploy/vox-export.sh                  # -> ./vox_export.jsonl
#   sudo ./prism/deploy/vox-export.sh /path/out.jsonl
#
# The file holds client conversations: keep it on the box (or wherever the
# register's own backups may go) and delete it when the analysis is done.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TREE="$(cd "$HERE/.." && pwd)"
OUT="${1:-vox_export.jsonl}"
COMPOSE="$TREE/deploy/compose/docker-compose.yml"
[[ -f "$COMPOSE" ]] || { echo "missing $COMPOSE" >&2; exit 1; }

detect_project() {
  local cid
  cid="$(docker ps -q --filter 'label=com.docker.compose.project' | head -1)"
  if [[ -n "$cid" ]]; then
    docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$cid"
  else
    echo compose
  fi
}
PROJECT="${PRISM_PROJECT:-$(detect_project)}"

SQL=$(cat <<'EOSQL'
COPY (
  SELECT json_build_object(
    'id', c.id, 'status', c.status, 'created_at', c.created_at,
    'recorder', c.recorder_email, 'recording_mode', c.recording_mode,
    'engine', c.engine, 'approved_engine', c.approved_engine, 'engine_alt', c.engine_alt,
    'language', c.language_detected, 'duration_seconds', c.duration_seconds,
    'prompt_version', c.prompt_version, 'registry_version', c.registry_version,
    'raw_transcript', c.raw_transcript, 'corrected_transcript', c.corrected_transcript,
    'structured_report', c.structured_report,
    'edits', COALESCE((SELECT json_agg(json_build_object(
                 'field_path', e.field_path, 'old_value', e.old_value, 'new_value', e.new_value,
                 'editor', e.editor_email, 'edited_at', e.edited_at) ORDER BY e.edited_at, e.id)
               FROM vox_conversation_edits e WHERE e.conversation_id = c.id), '[]'::json)
  )::text
  FROM vox_conversations c
  WHERE c.erased_at IS NULL AND c.status = 'submitted' AND c.structured_report IS NOT NULL
  ORDER BY c.created_at
) TO STDOUT
EOSQL
)

docker compose -p "$PROJECT" -f "$COMPOSE" exec -T postgres \
  psql -U prism -d register -v ON_ERROR_STOP=1 -Atc "$SQL" > "$OUT"
echo "wrote $(wc -l < "$OUT") approved conversations to $OUT"
echo "next: python3 $TREE/services/vocx/evals/vox_learn.py scorecard $OUT"
echo "      python3 $TREE/services/vocx/evals/vox_learn.py mine $OUT"

#!/usr/bin/env bash
#
# PRISM — upgrade / rollback for a Docker Compose deployment on one VM.
#
#   ./prism-deploy.sh upgrade  ~/prism-9bdda8b.zip   # backup → build → swap → verify
#   ./prism-deploy.sh rollback                       # back to the previous release
#   ./prism-deploy.sh rollback --with-db             # …and the database with it
#   ./prism-deploy.sh status                         # what is live, what can be rolled back to
#   ./prism-deploy.sh backup                         # a dump + secrets snapshot, nothing else
#   ./prism-deploy.sh verify                         # health-check the running stack
#   ./prism-deploy.sh restore-db <file.sql.gz>       # a specific dump, on purpose
#
# THE RULES THIS SCRIPT EXISTS TO ENFORCE
#
#   1. NOTHING IS TOUCHED UNTIL A BACKUP EXISTS. The database dump and the three secret
#      locations are captured, verified non-empty, and written OUTSIDE the release tree
#      before a single container is stopped.
#   2. THE NEW RELEASE IS BUILT BEFORE THE OLD ONE IS DISTURBED. A build failure — a bad
#      Dockerfile, a full disk, a network blip pulling a base image — leaves the running
#      stack untouched and exits non-zero. The swap only happens once every image exists.
#   3. FAILURE ROLLS ITSELF BACK. If the stack does not come back healthy inside
#      HEALTH_TIMEOUT, the script restores the previous tree and images itself rather
#      than leaving a half-deployed platform for someone to find.
#   4. THE PROJECT NAME IS PINNED. Compose derives it from the compose file's directory,
#      so it is stable across tree swaps — but it is passed explicitly anyway, because a
#      drifted project name would mean new containers pointed at new, empty volumes, and
#      the database would look wiped when it is merely orphaned.
#   5. VOLUMES ARE NEVER REMOVED. `down -v` appears nowhere in this file, and `down`
#      itself is not used — services are recreated in place.
#
# WHAT THIS DOES NOT DO: it never restores the database automatically. An upgrade that
# fails is a code problem; the data is fine, and silently rewinding it would destroy work
# the desk did between the backup and the failure. `rollback --with-db` exists for the
# rare case where you decide otherwise, and it dumps the current state first.

set -Eeuo pipefail

# ── configuration (override with environment variables) ──────────────────────
# WHERE THE DEPLOYMENT LIVES. The script must sit OUTSIDE the tree it swaps — a copy
# inside the release is replaced mid-run — so both placements are resolved: next to the
# `prism/` directory (the recommended home, and where `upgrade` installs it), or inside
# a checkout at deploy/prism-deploy.sh. PRISM_ROOT overrides either.
_self_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -n "${PRISM_ROOT:-}" ]]; then ROOT="$PRISM_ROOT"
elif [[ -d "$_self_dir/prism/deploy/compose" ]]; then ROOT="$_self_dir"
else ROOT="$(cd "$_self_dir/../.." && pwd)"; fi
LIVE="$ROOT/prism"                       # the tree compose runs from
RELEASES="$ROOT/releases"                # previous trees, newest last
BACKUPS="$ROOT/backups"                  # dumps + secret snapshots (never inside a tree)
PROFILES="${PRISM_PROFILES:-sso backup}" # compose profiles this deployment runs with
KEEP_RELEASES="${PRISM_KEEP_RELEASES:-3}"
KEEP_BACKUPS="${PRISM_KEEP_BACKUPS:-10}"
HEALTH_TIMEOUT="${PRISM_HEALTH_TIMEOUT:-180}"   # seconds to become healthy after a swap
MIN_FREE_GB="${PRISM_MIN_FREE_GB:-8}"

# The three secret locations that live INSIDE the tree and must survive a swap.
SECRET_PATHS=(deploy/compose/.env deploy/vocx-secrets deploy/nginx/certs)

STAMP="$(date +%Y%m%d-%H%M%S)"
LOCK="$ROOT/.prism-deploy.lock"
LOG="$BACKUPS/deploy-$STAMP.log"

# ── output ───────────────────────────────────────────────────────────────────
c_red=$'\033[31m'; c_grn=$'\033[32m'; c_ylw=$'\033[33m'; c_dim=$'\033[2m'; c_off=$'\033[0m'
say()  { printf '%s\n' "$*" | tee -a "$LOG" >&2; }
step() { say ""; say "${c_grn}▸ $*${c_off}"; }
warn() { say "${c_ylw}! $*${c_off}"; }
die()  { say "${c_red}✗ $*${c_off}"; exit 1; }
run()  { say "${c_dim}\$ $*${c_off}"; "$@" >>"$LOG" 2>&1; }

# ── compose plumbing ─────────────────────────────────────────────────────────
# The project name binds containers to the DATA VOLUMES. Compose derives it from the
# directory holding the first compose file (…/deploy/compose → "compose"), which is
# stable across tree swaps — but a wrong one here would create a second, empty stack and
# read as "the database is gone", so it is discovered from what is actually running and
# then passed explicitly on every call.
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

compose_files() {           # in a given tree, the -f flags that tree supports
  local tree="$1"
  # NOTE: two statements, deliberately. `local tree="$1" files=(… "$tree" …)` evaluates
  # the array BEFORE tree is assigned, and would silently build "-f /deploy/compose/…".
  local -a files
  files=(-f "$tree/deploy/compose/docker-compose.yml")
  [[ -f "$tree/deploy/compose/docker-compose.prod-posture.yml" ]] &&
    files+=(-f "$tree/deploy/compose/docker-compose.prod-posture.yml")
  printf '%s\n' "${files[@]}"
}
dc() {                      # docker compose, against a tree, with profiles + project
  local tree="$1"; shift
  local -a files profs
  mapfile -t files < <(compose_files "$tree")
  for p in $PROFILES; do profs+=(--profile "$p"); done
  docker compose -p "$PROJECT" "${files[@]}" "${profs[@]}" "$@"
}

edge_http_port() {          # the port the edge publishes, for the health probe
  local env="$LIVE/deploy/compose/.env" port=80
  [[ -f "$env" ]] && port="$(grep -E '^EDGE_HTTP_PORT=' "$env" | tail -1 | cut -d= -f2- || true)"
  echo "${port:-80}"
}

# ── preflight ────────────────────────────────────────────────────────────────
preflight() {
  step "Preflight"
  command -v docker >/dev/null || die "docker is not installed"
  docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing"
  docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon (permissions? service down?)"
  [[ -d "$LIVE" ]] || die "no live tree at $LIVE"
  [[ -f "$LIVE/deploy/compose/docker-compose.yml" ]] || die "$LIVE is not a PRISM tree"
  [[ -f "$LIVE/deploy/compose/.env" ]] || die "no deploy/compose/.env in the live tree — refusing to continue"
  mkdir -p "$BACKUPS" "$RELEASES"
  local free_gb
  free_gb="$(df -BG --output=avail "$ROOT" | tail -1 | tr -dc '0-9')"
  (( free_gb >= MIN_FREE_GB )) ||
    die "only ${free_gb}G free under $ROOT; need ${MIN_FREE_GB}G to build a release safely"
  say "  project=$PROJECT  profiles='$PROFILES'  free=${free_gb}G"
}

# ── backup ───────────────────────────────────────────────────────────────────
# A dump that is empty or truncated is worse than no dump, because it is trusted. The
# size is checked and gzip's own integrity test is run before anything else proceeds.
backup_db() {
  local dump="$BACKUPS/db-$STAMP.sql.gz"
  step "Backing up the database → $dump"
  if ! dc "$LIVE" ps --status running --services 2>/dev/null | grep -qx postgres; then
    die "postgres is not running — start the stack, or take the backup by hand, before upgrading"
  fi
  dc "$LIVE" exec -T postgres pg_dumpall -U prism 2>>"$LOG" | gzip > "$dump" || die "pg_dumpall failed"
  gzip -t "$dump" 2>>"$LOG" || die "the dump is corrupt (gzip -t failed): $dump"
  local bytes; bytes="$(stat -c%s "$dump")"
  (( bytes > 100000 )) || die "the dump is only ${bytes} bytes — refusing to treat that as a backup"
  say "  $(du -h "$dump" | cut -f1) written and verified"
  echo "$dump"
}

backup_secrets() {
  local tarball="$BACKUPS/secrets-$STAMP.tar.gz"
  step "Snapshotting the secret locations → $tarball"
  local -a present=()
  for p in "${SECRET_PATHS[@]}"; do [[ -e "$LIVE/$p" ]] && present+=("$p"); done
  (( ${#present[@]} )) || die "none of the secret paths exist in $LIVE"
  tar -czf "$tarball" -C "$LIVE" "${present[@]}" 2>>"$LOG" || die "could not archive the secrets"
  chmod 600 "$tarball"
  say "  ${#present[@]} location(s): ${present[*]}"
  echo "$tarball"
}

# Tag the CURRENT images so a rollback never has to rebuild. A tagged image is not
# dangling, so `docker image prune` leaves it alone — which is the whole point: the
# rollback path must not depend on an untagged layer nobody promised to keep.
snapshot_images() {
  step "Tagging the running images for rollback"
  local map="$BACKUPS/images-$STAMP.tsv" n=0
  : > "$map"
  local cid svc img
  for cid in $(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT"); do
    svc="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$cid")"
    img="$(docker inspect -f '{{.Image}}' "$cid")"            # the resolved image ID
    [[ -n "$svc" && -n "$img" ]] || continue
    local name; name="$(docker inspect -f '{{.Config.Image}}' "$cid")"
    docker tag "$img" "prism-rollback/$svc:$STAMP" >>"$LOG" 2>&1 || continue
    printf '%s\t%s\t%s\n' "$svc" "$name" "prism-rollback/$svc:$STAMP" >> "$map"
    n=$((n+1))
  done
  (( n )) || warn "no running containers found for project '$PROJECT' — is the stack up?"
  say "  $n image(s) tagged as prism-rollback/<service>:$STAMP"
  echo "$map"
}

restore_images() {          # re-point the compose image names at the tagged snapshots
  local map="$1"
  [[ -f "$map" ]] || { warn "no image map at $map — rollback will rebuild instead"; return 1; }
  local svc name tag ok=0
  while IFS=$'\t' read -r svc name tag; do
    if docker image inspect "$tag" >/dev/null 2>&1; then
      docker tag "$tag" "$name" >>"$LOG" 2>&1 && ok=$((ok+1))
    else
      warn "rollback image missing for $svc ($tag)"
      return 1
    fi
  done < "$map"
  say "  $ok image(s) restored from the snapshot"
}

# ── health ───────────────────────────────────────────────────────────────────
# Two questions, both of which must be yes: does every container that declares a
# healthcheck report healthy, and does a real request survive the whole edge → gateway
# path? Container state alone would call a stack healthy that answers 502 at the door.
health_once() {
  local port; port="$(edge_http_port)"
  local cid state unhealthy=0
  for cid in $(docker ps -q --filter "label=com.docker.compose.project=$PROJECT"); do
    state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid")"
    [[ "$state" == "unhealthy" || "$state" == "starting" ]] && unhealthy=$((unhealthy+1))
  done
  (( unhealthy == 0 )) || return 1
  curl -sf -m 10 "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1 || return 1
  return 0
}

wait_healthy() {
  step "Waiting for the stack to report healthy (up to ${HEALTH_TIMEOUT}s)"
  local deadline=$((SECONDS + HEALTH_TIMEOUT))
  while (( SECONDS < deadline )); do
    if health_once; then say "  ${c_grn}healthy${c_off}"; return 0; fi
    sleep 5
  done
  warn "still not healthy after ${HEALTH_TIMEOUT}s"
  dc "$LIVE" ps 2>&1 | tee -a "$LOG" >&2 || true
  return 1
}

# ── prune ────────────────────────────────────────────────────────────────────
prune_old() {
  local n
  n="$(find "$RELEASES" -maxdepth 1 -mindepth 1 -type d | wc -l)"
  if (( n > KEEP_RELEASES )); then
    find "$RELEASES" -maxdepth 1 -mindepth 1 -type d -printf '%T@ %p\n' | sort -n |
      head -n "$(( n - KEEP_RELEASES ))" | cut -d' ' -f2- |
      while read -r d; do say "  removing old release $(basename "$d")"; rm -rf "$d"; done
  fi
  ls -1t "$BACKUPS"/db-*.sql.gz 2>/dev/null | tail -n "+$((KEEP_BACKUPS + 1))" | xargs -r rm -f
}

# ── commands ─────────────────────────────────────────────────────────────────
cmd_upgrade() {
  local zip="${1:-}"
  [[ -n "$zip" ]] || die "usage: $0 upgrade <prism-<hash>.zip>"
  [[ -f "$zip" ]] || die "no such file: $zip"
  preflight
  command -v unzip >/dev/null || die "unzip is not installed"

  local db_backup secrets_backup image_map
  db_backup="$(backup_db)"
  secrets_backup="$(backup_secrets)"
  image_map="$(snapshot_images)"

  # Unpack into a staging tree. The zip contains a top-level `prism/` directory.
  local rel; rel="$RELEASES/$STAMP-$(basename "$zip" .zip)"
  step "Unpacking $(basename "$zip") → $rel"
  rm -rf "$rel.tmp"; mkdir -p "$rel.tmp"
  run unzip -q "$zip" -d "$rel.tmp"
  local inner; inner="$(find "$rel.tmp" -maxdepth 2 -name docker-compose.yml -path '*/deploy/compose/*' | head -1)"
  [[ -n "$inner" ]] || inner="$(find "$rel.tmp" -maxdepth 4 -name docker-compose.yml -path '*/deploy/compose/*' | head -1)"
  [[ -n "$inner" ]] || die "that zip has no deploy/compose/docker-compose.yml — wrong archive?"
  local newtree; newtree="$(cd "$(dirname "$inner")/../.." && pwd)"
  mv "$newtree" "$rel"; rm -rf "$rel.tmp"

  step "Restoring the secrets into the new tree"
  run tar -xzf "$secrets_backup" -C "$rel"
  for p in "${SECRET_PATHS[@]}"; do
    [[ -e "$rel/$p" ]] || warn "secret path missing after restore: $p"
  done
  [[ -f "$rel/deploy/compose/.env" ]] || die "the new tree has no .env after restore — stopping"

  # BUILD BEFORE SWAPPING. A failure here must leave the running stack untouched.
  step "Building the new images (the live stack keeps serving)"
  if ! dc "$rel" build; then
    warn "build failed — the running deployment was NOT touched"
    say "  full log: $LOG"
    rm -rf "$rel"
    exit 1
  fi

  step "Swapping the tree in"
  local prev="$RELEASES/previous-$STAMP"
  mv "$LIVE" "$prev"
  mv "$rel" "$LIVE"
  ln -sfn "$prev" "$RELEASES/.previous"
  printf '%s\n' "$db_backup" > "$RELEASES/.previous-db"
  printf '%s\n' "$image_map"  > "$RELEASES/.previous-images"

  step "Starting the new release"
  if ! dc "$LIVE" up -d; then
    warn "compose up failed — rolling back"
    do_rollback ""; exit 1
  fi

  if ! wait_healthy; then
    warn "the new release did not become healthy — rolling back automatically"
    do_rollback ""
    die "rolled back to the previous release. Log: $LOG"
  fi

  prune_old
  step "${c_grn}Upgrade complete${c_off}"
  say "  live      : $LIVE"
  say "  previous  : $prev   (./prism-deploy.sh rollback)"
  say "  db backup : $db_backup"
  say "  log       : $LOG"
  say ""
  say "  Hard-refresh the browser (Ctrl+Shift+R) — the UI is a cached bundle."
}

do_rollback() {             # $1: "--with-db" to restore the pre-upgrade dump as well
  local with_db="${1:-}"
  local prev; prev="$(readlink -f "$RELEASES/.previous" 2>/dev/null || true)"
  [[ -n "$prev" && -d "$prev" ]] || die "no previous release recorded — nothing to roll back to"

  step "Rolling back to $(basename "$prev")"
  local failed="$RELEASES/failed-$STAMP"
  [[ -d "$LIVE" ]] && mv "$LIVE" "$failed"
  mv "$prev" "$LIVE"
  rm -f "$RELEASES/.previous"

  local map; map="$(cat "$RELEASES/.previous-images" 2>/dev/null || true)"
  if [[ -n "$map" ]] && restore_images "$map"; then
    run dc "$LIVE" up -d --no-build || dc "$LIVE" up -d --build
  else
    warn "image snapshot unusable — rebuilding the previous release (slower, same result)"
    dc "$LIVE" up -d --build
  fi

  if [[ "$with_db" == "--with-db" ]]; then
    local dump; dump="$(cat "$RELEASES/.previous-db" 2>/dev/null || true)"
    [[ -f "$dump" ]] || die "no pre-upgrade dump recorded; restore by hand with restore-db"
    warn "restoring the database from $dump — everything written since then will be lost"
    cmd_restore_db "$dump"
  fi

  wait_healthy || warn "the previous release is up but not reporting healthy — check: dc ps / logs"
  step "${c_grn}Rollback complete${c_off}"
  say "  live       : $LIVE"
  say "  failed one : $failed  (kept for inspection)"
}

cmd_restore_db() {
  local dump="${1:-}"
  [[ -f "$dump" ]] || die "usage: $0 restore-db <file.sql.gz>"
  gzip -t "$dump" || die "that dump is corrupt"
  preflight

  # A restore REPLACES. Capture the present first so this step is itself reversible.
  local now="$BACKUPS/db-before-restore-$STAMP.sql.gz"
  step "Dumping the CURRENT database first → $now"
  dc "$LIVE" exec -T postgres pg_dumpall -U prism 2>>"$LOG" | gzip > "$now"
  gzip -t "$now" || die "could not take a safety dump — refusing to restore over it"

  # Every service holding a connection must be down: pg_dumpall --clean drops the
  # databases, and an open session makes the DROP fail half-way through.
  local writers=(register access gateway atlas vocx pulse workflows notifier orchestrator temporal temporal-ui)
  step "Stopping the services that hold connections"
  run dc "$LIVE" stop "${writers[@]}" || true

  step "Restoring $dump"
  if ! gunzip -c "$dump" | dc "$LIVE" exec -T postgres psql -U prism -d postgres >>"$LOG" 2>&1; then
    warn "the restore reported errors — see $LOG"
    warn "the pre-restore state is at $now"
  fi

  step "Starting the services again"
  run dc "$LIVE" start "${writers[@]}" || dc "$LIVE" up -d
  wait_healthy || warn "not healthy after the restore — check the log"
  say "  safety dump: $now"
}

cmd_status() {
  preflight
  step "Live"
  say "  tree    : $LIVE"
  say "  project : $PROJECT"
  dc "$LIVE" ps 2>&1 | tee -a "$LOG" >&2 || true
  step "Rollback target"
  local prev; prev="$(readlink -f "$RELEASES/.previous" 2>/dev/null || true)"
  [[ -n "$prev" && -d "$prev" ]] && say "  $prev" || say "  (none recorded)"
  step "Backups (newest first)"
  ls -1t "$BACKUPS"/db-*.sql.gz 2>/dev/null | head -5 |
    while read -r f; do say "  $(du -h "$f" | cut -f1)\t$f"; done || say "  (none)"
  step "Data volumes"
  docker volume ls --filter "name=${PROJECT}_" --format '  {{.Name}}' | tee -a "$LOG" >&2 || true
}

cmd_verify() {
  preflight
  if health_once; then say "${c_grn}healthy${c_off}"; else wait_healthy || die "not healthy"; fi
}

# ── entry ────────────────────────────────────────────────────────────────────
mkdir -p "$BACKUPS"
exec 9>"$LOCK"
flock -n 9 || die "another deploy is running (lock: $LOCK)"
trap 'say "${c_red}✗ aborted at line $LINENO${c_off} — log: $LOG"' ERR

case "${1:-}" in
  upgrade)    shift; cmd_upgrade "$@" ;;
  rollback)   shift; preflight; do_rollback "${1:-}" ;;
  restore-db) shift; cmd_restore_db "$@" ;;
  backup)     preflight; backup_db >/dev/null; backup_secrets >/dev/null; snapshot_images >/dev/null
              say "${c_grn}backup complete${c_off} → $BACKUPS" ;;
  status)     cmd_status ;;
  verify)     cmd_verify ;;
  *) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 1 ;;
esac

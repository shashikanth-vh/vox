#!/usr/bin/env bash
#
# PRISM — upgrade / rollback for a Docker Compose deployment on one VM.
#
#   ./prism-deploy.sh upgrade  ~/prism-9bdda8b.zip   # backup → build → swap → verify
#                                                    # .tar / .tar.gz / .tgz also accepted
#   ./prism-deploy.sh rollback                       # back to the previous release
#   ./prism-deploy.sh rollback --with-db             # …and the database with it
#   ./prism-deploy.sh status                         # what is live, what can be rolled back to
#   ./prism-deploy.sh backup                         # dump + document store + secrets, nothing else
#   ./prism-deploy.sh verify                         # health-check the running stack
#   ./prism-deploy.sh restore-db <file.sql.gz>       # a specific dump, on purpose
#   ./prism-deploy.sh restore-files <minio-*.tar.gz> # the document store (restore WITH its dump)
#   ./prism-deploy.sh restore-secrets <secrets-*.tar.gz> # .env + VocX secrets + TLS certs
#   ./prism-deploy.sh restore-plan                   # what protects this system, what exists,
#                                                    # and exactly which files to restore from
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

INVOCATION="$*"             # what the operator actually typed, for copy-pasteable advice
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

# ── release archives ─────────────────────────────────────────────────────────
# zip, tar, tar.gz and tgz are all accepted, and the KIND IS SNIFFED FROM THE CONTENT
# rather than the extension: a release renamed by a mail client or re-wrapped by someone
# passing it along is still the same release, and refusing it on the strength of its
# name would be theatre. GNU tar detects its own compression, so one branch covers the
# three tar spellings.
archive_kind() {
  local f="$1"
  if tar -tf "$f" >/dev/null 2>&1; then echo tar
  elif command -v unzip >/dev/null && unzip -l "$f" >/dev/null 2>&1; then echo zip
  else die "$(basename "$f") is neither a readable tar nor a zip (corrupt, or not a release archive)"
  fi
}

unpack() {                  # unpack "$archive" "$dest"
  local f="$1" dest="$2"
  case "$(archive_kind "$f")" in
    tar) run tar -xf "$f" -C "$dest" ;;
    zip) command -v unzip >/dev/null || die "unzip is not installed (needed for a .zip release)"
         run unzip -q "$f" -d "$dest" ;;
  esac
}

release_name() {            # a directory-safe name, with every archive suffix stripped
  local b; b="$(basename "$1")"
  b="${b%.zip}"; b="${b%.tgz}"; b="${b%.tar}"; b="${b%.gz}"; b="${b%.tar}"
  echo "$b"
}

edge_http_port() {          # the port the edge publishes, for the health probe
  local env="$LIVE/deploy/compose/.env" port=80
  [[ -f "$env" ]] && port="$(grep -E '^EDGE_HTTP_PORT=' "$env" | tail -1 | cut -d= -f2- || true)"
  echo "${port:-80}"
}

# ── schema ───────────────────────────────────────────────────────────────────
# Migrations run FORWARD by themselves: register and access both boot through
# `alembic upgrade head` before they serve, so a release carrying a new revision applies
# it during the `up`. There is no reverse gear — this script never runs `downgrade`, and
# a rollback therefore leaves the NEW schema under the OLD code. That is usually fine
# (an added column the old code ignores) and occasionally not (a dropped or renamed one).
# Either way the operator should learn it BEFORE the swap, not while rolling back.
migration_delta() {         # revisions present in $2 but not in $1
  local old_tree="$1" new_tree="$2" d
  for d in services/register/migrations/versions services/access/migrations/versions; do
    [[ -d "$new_tree/$d" ]] || continue
    local f
    for f in "$new_tree/$d"/*.py; do
      [[ -e "$f" ]] || continue
      [[ -e "$old_tree/$d/$(basename "$f")" ]] || echo "${d%%/migrations*}: $(basename "$f")"
    done
  done
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
  mkdir -p "$BACKUPS" "$RELEASES" 2>/dev/null ||
    die "cannot create $BACKUPS / $RELEASES as $(id -un) — run the deploy with sudo"
  # AND THE TREES MUST BE MOVABLE. Swapping releases is two `mv`s inside $ROOT, so write
  # permission there is as load-bearing as read permission on the secrets — and finding
  # out at the swap means a full build was spent first.
  local -a needwrite=("$ROOT" "$BACKUPS" "$RELEASES")
  local d
  for d in "${needwrite[@]}"; do
    [[ -w "$d" ]] || die "cannot write to $d as $(id -un) — run the deploy with sudo, or fix ownership of $ROOT"
  done

  # THE SECRETS MUST BE READABLE BY WHOEVER IS RUNNING THIS. A TLS key is root-owned and
  # mode 600 by every sane convention, so an unprivileged run cannot snapshot it — and
  # discovering that AFTER the database dump, as this script used to, wastes the operator's
  # nerve at the exact moment they are watching a production deploy. Checked first, named
  # precisely, with both ways out.
  local unreadable
  unreadable="$(find "${SECRET_PATHS[@]/#/$LIVE/}" ! -readable -print 2>/dev/null || true)"
  if [[ -n "$unreadable" ]]; then
    say "${c_red}✗ these secret files cannot be read as $(id -un):${c_off}"
    say "$(printf '%s\n' "$unreadable" | sed 's/^/    /')"
    say ""
    say "  Run the whole deploy with sudo (simplest, and what the certs expect):"
    say "      sudo $0 ${INVOCATION:-upgrade <archive>}"
    say "  …or hand ownership to your user once:"
    say "      sudo chown -R $(id -un):$(id -gn) $LIVE/deploy/nginx/certs $LIVE/deploy/vocx-secrets"
    die "refusing to continue — a snapshot that silently skipped a key would restore a tree that cannot serve TLS"
  fi

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
  # --clean --if-exists: the dump carries its own DROPs, so restore-db can load it
  # over a cluster that already has databases. Without them a restore over existing
  # data is a silent no-op — every CREATE/COPY collides, psql exits 0 anyway, and
  # the operator is left staring at the OLD data believing it is the new.
  dc "$LIVE" exec -T postgres pg_dumpall -U prism --clean --if-exists 2>>"$LOG" | gzip > "$dump" || die "pg_dumpall failed"
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
  if ! tar -czf "$tarball" -C "$LIVE" "${present[@]}" 2>>"$LOG"; then
    say "$(tail -5 "$LOG" | sed 's/^/    /')"
    die "could not archive the secrets — see above and $LOG"
  fi
  chmod 600 "$tarball"
  say "  ${#present[@]} location(s): ${present[*]}"
  echo "$tarball"
}

# THE DOCUMENT BYTES LIVE IN MINIO — the register holds references, the objects hold
# the sanction letters, CAMs and evidence files. A database backup without the object
# store restores a book that swears documents are on file which no longer exist, so the
# store is captured with the same guarantees as the dump: verified archive, written
# outside the tree, rotated on the same schedule. MinIO renames objects into place
# atomically, so a tar of the live volume sees each object whole (old or new, never
# half) — good enough for a nightly cadence on a document store.
backup_files() {
  local tarball="$BACKUPS/minio-$STAMP.tar.gz"
  local vol; vol="$(docker volume ls -q | grep -x "${PROJECT}_miniodata" || true)"
  if [[ -z "$vol" ]]; then
    warn "no ${PROJECT}_miniodata volume — document store not captured (inline storage?)"
    return 0
  fi
  step "Backing up the document store ($vol) → $tarball"
  docker run --rm -v "$vol":/data:ro -v "$BACKUPS":/out alpine \
    tar -czf "/out/$(basename "$tarball")" -C /data . 2>>"$LOG" || die "document-store backup failed"
  gzip -t "$tarball" 2>>"$LOG" || die "the document archive is corrupt (gzip -t failed): $tarball"
  local bytes; bytes="$(stat -c%s "$tarball")"
  (( bytes > 500 )) || warn "the document archive is only ${bytes} bytes — is the store empty?"
  say "  $(du -h "$tarball" | cut -f1) written and verified"
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
# THE EDGE CACHES THE IPs OF EVERYTHING BEHIND IT. nginx.conf declares static upstreams
# (`upstream ui { server ui:80; }`), and nginx resolves those names ONCE at startup, for
# the life of the worker. An upgrade recreates the ui and gateway containers, Docker
# hands them new addresses, and nginx — whose own image did not change, so it was never
# recreated — keeps dialling the old ones. Every page then 502s with "connect() failed
# (111: Connection refused)" against a container that no longer exists, while every
# container reports perfectly healthy. A reload re-reads the config and re-resolves,
# without dropping a connection; a restart is the fallback if the reload is refused.
reload_edge() {
  dc "$LIVE" ps --status running --services 2>/dev/null | grep -qx nginx || return 0
  step "Reloading the edge so it re-resolves the recreated containers"
  if dc "$LIVE" exec -T nginx nginx -s reload >>"$LOG" 2>&1; then
    say "  reloaded"
  else
    warn "reload refused — restarting nginx instead"
    run dc "$LIVE" restart nginx || warn "could not restart nginx; check it by hand"
  fi
}

health_once() {
  local port; port="$(edge_http_port)"
  local cid state unhealthy=0
  for cid in $(docker ps -q --filter "label=com.docker.compose.project=$PROJECT"); do
    state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$cid")"
    [[ "$state" == "unhealthy" || "$state" == "starting" ]] && unhealthy=$((unhealthy+1))
  done
  (( unhealthy == 0 )) || return 1
  # /healthz proves the GATEWAY lane. It says nothing about the UI, which nginx reaches
  # through a different upstream — and a stranded ui upstream passed this gate happily
  # while every page in the browser answered 502. Probe both doors.
  curl -sf -m 10 "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1 || return 1
  curl -sf -m 10 -o /dev/null "http://127.0.0.1:${port}/ui/" 2>/dev/null || return 1
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
  # THE ROLLBACK TARGET IS NEVER PRUNED. Trimming by age alone will eventually delete
  # the tree `.previous` points at — and the failure is silent until the day someone
  # needs it, when rollback reports "nothing to roll back to" while the symlink sits
  # there pointing at a directory that no longer exists. Whatever else goes, that stays.
  local keep; keep="$(readlink -f "$RELEASES/.previous" 2>/dev/null || true)"
  local -a candidates=()
  local d
  while IFS= read -r d; do
    [[ -n "$keep" && "$d" == "$keep" ]] && continue
    candidates+=("$d")
  done < <(find "$RELEASES" -maxdepth 1 -mindepth 1 -type d -printf '%T@ %p\n' |
           sort -n | cut -d' ' -f2-)
  local n=${#candidates[@]}
  if (( n > KEEP_RELEASES )); then
    for d in "${candidates[@]:0:$((n - KEEP_RELEASES))}"; do
      say "  removing old release $(basename "$d")"
      rm -rf "$d"
    done
  fi
  ls -1t "$BACKUPS"/db-*.sql.gz 2>/dev/null | tail -n "+$((KEEP_BACKUPS + 1))" | xargs -r rm -f
  ls -1t "$BACKUPS"/minio-*.tar.gz 2>/dev/null | tail -n "+$((KEEP_BACKUPS + 1))" | xargs -r rm -f

  # DOCKER IMAGES accumulate the same way the release trees did: every upgrade tags
  # ~18 prism-rollback/<service>:<stamp> images and each rebuild orphans the previous
  # build's layers. Keep EXACTLY the rollback stamp that matches the .previous tree —
  # the one `rollback` would restore — and drop the older stamps, then the dangling
  # layers. Deliberately conservative: if the matching stamp cannot be confirmed among
  # the tags, nothing is deleted (an over-eager trim here is how a rollback finds
  # nothing to roll back to). The build CACHE is untouched — it is what keeps the next
  # upgrade fast.
  local keep_stamp=""
  if [[ -n "$keep" ]]; then
    keep_stamp="$(basename "$keep")"; keep_stamp="${keep_stamp#previous-}"
  fi
  if [[ -n "$keep_stamp" ]] && docker images "prism-rollback/*" --format '{{.Tag}}' \
      2>/dev/null | grep -qx "$keep_stamp"; then
    local trimmed
    trimmed=$(docker images "prism-rollback/*" --format '{{.Repository}}:{{.Tag}}' \
      | grep -v ":${keep_stamp}\$" | tee -a "$LOG" | wc -l)
    docker images "prism-rollback/*" --format '{{.Repository}}:{{.Tag}}' \
      | grep -v ":${keep_stamp}\$" | xargs -r docker rmi >>"$LOG" 2>&1 || true
    (( trimmed > 0 )) && say "  removed $trimmed old rollback image tag(s) (kept :$keep_stamp)"
  fi
  docker image prune -f >>"$LOG" 2>&1 || true
}

# ── commands ─────────────────────────────────────────────────────────────────
cmd_upgrade() {
  local archive="${1:-}"
  [[ -n "$archive" ]] || die "usage: $0 upgrade <release archive: .zip | .tar | .tar.gz | .tgz>"
  [[ -f "$archive" ]] || die "no such file: $archive"
  preflight
  local kind; kind="$(archive_kind "$archive")"
  say "  release archive: $(basename "$archive") ($kind, $(du -h "$archive" | cut -f1))"

  local db_backup files_backup secrets_backup image_map
  db_backup="$(backup_db)"
  files_backup="$(backup_files)"
  secrets_backup="$(backup_secrets)"
  image_map="$(snapshot_images)"

  # Unpack into a staging tree. The zip contains a top-level `prism/` directory.
  local rel; rel="$RELEASES/$STAMP-$(release_name "$archive")"
  step "Unpacking $(basename "$archive") → $rel"
  rm -rf "$rel.tmp"; mkdir -p "$rel.tmp"
  unpack "$archive" "$rel.tmp"
  local inner; inner="$(find "$rel.tmp" -maxdepth 2 -name docker-compose.yml -path '*/deploy/compose/*' | head -1)"
  [[ -n "$inner" ]] || inner="$(find "$rel.tmp" -maxdepth 4 -name docker-compose.yml -path '*/deploy/compose/*' | head -1)"
  [[ -n "$inner" ]] || die "that archive has no deploy/compose/docker-compose.yml — wrong file?"
  local newtree; newtree="$(cd "$(dirname "$inner")/../.." && pwd)"
  mv "$newtree" "$rel"; rm -rf "$rel.tmp"

  step "Restoring the secrets into the new tree"
  run tar -xzf "$secrets_backup" -C "$rel"
  for p in "${SECRET_PATHS[@]}"; do
    [[ -e "$rel/$p" ]] || warn "secret path missing after restore: $p"
  done
  [[ -f "$rel/deploy/compose/.env" ]] || die "the new tree has no .env after restore — stopping"

  # SAY IT NOW, not later. A release that carries new revisions changes what a rollback
  # means, and the moment to know that is before anything moves.
  local schema; schema="$(migration_delta "$LIVE" "$rel")"
  if [[ -n "$schema" ]]; then
    step "This release carries SCHEMA CHANGES"
    say "$(printf '%s\n' "$schema" | sed 's/^/    /')"
    say ""
    say "  They apply automatically when the services start (alembic upgrade head)."
    say "  ${c_ylw}A plain rollback does NOT undo them${c_off} — it puts the old code on the new"
    say "  schema. That is safe for an added column and unsafe for a dropped one."
    say "  If you need the old schema back too: ./prism-deploy.sh rollback --with-db"
  else
    say ""
    say "  No schema changes in this release — rollback is fully symmetric."
  fi

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
  printf '%s\n' "$files_backup" > "$RELEASES/.previous-files"
  printf '%s\n' "$image_map"  > "$RELEASES/.previous-images"
  printf '%s\n' "$schema"     > "$RELEASES/.previous-migrations"

  step "Starting the new release"
  if ! dc "$LIVE" up -d; then
    warn "compose up failed — rolling back"
    do_rollback ""; exit 1
  fi
  reload_edge

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
  if [[ -z "$prev" || ! -L "$RELEASES/.previous" ]]; then
    die "no previous release recorded — nothing to roll back to"
  fi
  # A recorded-but-missing target is a different fault from never having upgraded, and
  # it needs a different answer: the tree is gone, so the way back is the release
  # archive plus a database restore, not this command.
  [[ -d "$prev" ]] || die "the recorded previous release is MISSING: $prev
   Roll back by re-running 'upgrade' with the older release archive.
   Its database backup is under $BACKUPS (see: $0 status)."

  step "Rolling back to $(basename "$prev")"
  local failed="$RELEASES/failed-$STAMP"
  [[ -d "$LIVE" ]] && mv "$LIVE" "$failed"
  mv "$prev" "$LIVE"
  rm -f "$RELEASES/.previous"

  local schema; schema="$(cat "$RELEASES/.previous-migrations" 2>/dev/null || true)"
  if [[ -n "${schema// /}" && "$with_db" != "--with-db" ]]; then
    warn "the release being rolled back ADDED schema revisions:"
    say "$(printf '%s\n' "$schema" | sed 's/^/    /')"
    warn "the database keeps them — the old code will run against the newer schema."
    warn "if that release dropped or renamed anything, stop and use: $0 rollback --with-db"
  fi

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
    # The document store goes back WITH its database: the rows reference the objects,
    # and restoring one without the other leaves a book pointing at files that are
    # newer or missing.
    local files; files="$(cat "$RELEASES/.previous-files" 2>/dev/null || true)"
    if [[ -f "$files" ]]; then
      warn "restoring the document store from $files to match the dump"
      cmd_restore_files "$files"
    fi
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

  # Drop every application database FIRST, whatever the dump carries. Older dumps
  # were taken without --clean; loaded over existing databases they collide on every
  # CREATE/COPY, psql exits 0 anyway, and the operator is left staring at the OLD
  # data believing it is the new. Pre-dropping makes the restore deterministic for
  # any dump vintage ('postgres' and the templates stay — the dump reconnects
  # through them).
  step "Dropping the application databases (the safety dump above is the way back)"
  local _dbs; _dbs="$(dc "$LIVE" exec -T postgres psql -U prism -d postgres -Atc \
    "SELECT datname FROM pg_database WHERE datname NOT IN ('postgres','template0','template1')" 2>>"$LOG")"
  local _db
  for _db in $_dbs; do
    dc "$LIVE" exec -T postgres psql -U prism -d postgres \
      -c "DROP DATABASE IF EXISTS \"$_db\" WITH (FORCE);" >>"$LOG" 2>&1 ||
      die "could not drop database '$_db' — nothing has been restored; see $LOG"
  done

  step "Restoring $dump"
  if ! gunzip -c "$dump" | dc "$LIVE" exec -T postgres psql -U prism -d postgres >>"$LOG" 2>&1; then
    warn "the restore reported errors — see $LOG"
    warn "the pre-restore state is at $now"
  fi

  # A cross-box restore carries the SOURCE box's role password (pg_dumpall includes
  # ALTER ROLE). This tree's services authenticate with the .env password — re-assert
  # it so a production dump cannot lock a test box out of its own database.
  local _pw
  _pw="$(grep -E '^PRISM_DB_PASSWORD=' "$LIVE/deploy/compose/.env" 2>/dev/null |
         head -1 | cut -d= -f2- | sed 's/[[:space:]]*#.*$//' | tr -d '"' | xargs || true)"
  _pw="${_pw:-prism}"
  dc "$LIVE" exec -T postgres psql -U prism -d postgres \
    -v pw="$_pw" -c "ALTER ROLE prism PASSWORD :'pw';" >>"$LOG" 2>&1 ||
    warn "could not re-assert the prism role password — services may fail to connect; see $LOG"

  step "Starting the services again"
  run dc "$LIVE" start "${writers[@]}" || dc "$LIVE" up -d
  wait_healthy || warn "not healthy after the restore — check the log"
  # nginx resolves its upstreams once at start — after the writers came back on new
  # addresses the edge must look again, or the UI reports 'cannot reach' services
  # that are perfectly healthy.
  run dc "$LIVE" restart nginx || true
  say "  safety dump: $now"
}

cmd_restore_files() {
  local tarball="${1:-}"
  [[ -f "$tarball" ]] || die "usage: $0 restore-files <minio-*.tar.gz>"
  gzip -t "$tarball" || die "that archive is corrupt"
  preflight
  local vol; vol="$(docker volume ls -q | grep -x "${PROJECT}_miniodata" || true)"
  [[ -n "$vol" ]] || die "no ${PROJECT}_miniodata volume — is the stack initialised?"

  # A restore REPLACES. Snapshot the present store first so this step is itself
  # reversible, exactly as restore-db does with its safety dump.
  local now="$BACKUPS/minio-before-restore-$STAMP.tar.gz"
  step "Snapshotting the CURRENT document store first → $now"
  docker run --rm -v "$vol":/data:ro -v "$BACKUPS":/out alpine \
    tar -czf "/out/$(basename "$now")" -C /data . 2>>"$LOG" ||
    die "could not take a safety snapshot — refusing to restore over the store"

  # MinIO (and the register, which writes through it) must be quiet while the volume
  # is emptied and refilled — a write racing the untar corrupts an object.
  step "Stopping MinIO and its writers"
  run dc "$LIVE" stop minio register workflows || true

  step "Restoring $tarball"
  local abs; abs="$(readlink -f "$tarball")"
  if ! docker run --rm -v "$vol":/data -v "$abs":/restore.tar.gz:ro alpine \
      sh -c 'find /data -mindepth 1 -delete && tar -xzf /restore.tar.gz -C /data' 2>>"$LOG"; then
    warn "the restore reported errors — see $LOG"
    warn "the pre-restore store is at $now"
  fi

  step "Starting the services again"
  run dc "$LIVE" start minio register workflows || dc "$LIVE" up -d
  wait_healthy || warn "not healthy after the restore — check the log"
  # nginx resolves upstreams once at start — the restarted writers may be on new
  # addresses, so the edge looks again (same lesson as restore-db).
  run dc "$LIVE" restart nginx || true
  say "  safety snapshot: $now"
  say "  restore the MATCHING database dump too if you have not — the rows and the"
  say "  objects reference each other and must come from the same moment."
}

cmd_restore_secrets() {
  local tarball="${1:-}"
  [[ -f "$tarball" ]] || die "usage: $0 restore-secrets <secrets-*.tar.gz>"
  gzip -t "$tarball" || die "that snapshot is corrupt"
  [[ -d "$LIVE" ]] || die "no live tree at $LIVE to restore into"

  # A restore REPLACES. If anything is there now, snapshot it first — same contract as
  # restore-db and restore-files: the restore is itself reversible.
  local p present=0
  for p in "${SECRET_PATHS[@]}"; do [[ -e "$LIVE/$p" ]] && present=1; done
  (( present )) && backup_secrets >/dev/null

  step "Restoring the secret locations from $(basename "$tarball") → $LIVE"
  say "$(tar -tzf "$tarball" | head -12 | sed 's/^/    /')"
  run tar -xzf "$tarball" -C "$LIVE"
  for p in "${SECRET_PATHS[@]}"; do
    [[ -e "$LIVE/$p" ]] || warn "still missing after the restore: $p"
  done

  # .env applies at container CREATION and the certs at nginx startup — restoring the
  # files alone changes nothing running. Recreate what differs and re-resolve the edge.
  if docker ps -q --filter "label=com.docker.compose.project=$PROJECT" 2>/dev/null | grep -q .; then
    step "Re-applying (recreate against the restored .env, reload the edge)"
    run dc "$LIVE" up -d || warn "compose up failed — check $LOG"
    reload_edge
  else
    say "  stack not running — the restored secrets apply on the next start"
  fi
}

# ── the DR answer sheet ──────────────────────────────────────────────────────
# Run at 2am with the stack half-dead, this must still answer: every probe is guarded,
# nothing dies, and what cannot be read becomes a warning inside the report.
cmd_restore_plan() {
  step "How this deployment is protected"
  say "  live tree        : $LIVE"
  say "  backups (host)   : $BACKUPS"
  say "                     written by 'backup' and by EVERY 'upgrade' (before anything moves);"
  say "                     keeps the newest $KEEP_BACKUPS of each family"
  local vol vpath="" keep_days=""
  vol="$(docker volume ls -q 2>/dev/null | grep -x "${PROJECT}_pgbackups" || true)"
  [[ -n "$vol" ]] && vpath="$(docker volume inspect -f '{{.Mountpoint}}' "$vol" 2>/dev/null || true)"
  [[ -f "$LIVE/deploy/compose/.env" ]] &&
    keep_days="$(grep -E '^PGBACKUP_KEEP=' "$LIVE/deploy/compose/.env" | tail -1 | cut -d= -f2- || true)"
  if [[ -n "$vpath" ]]; then
    say "  nightly (volume) : $vpath"
    say "                     pgbackup (database) + filebackup (documents) sidecars, DAILY,"
    say "                     keeping ${keep_days:-14} day(s)"
  else
    warn "nightly: no ${PROJECT}_pgbackups volume found — are the sidecars (profile 'backup') running?"
  fi
  local cronline
  cronline="$(crontab -l 2>/dev/null | grep -F 'prism-offsite.sh sync' || true)"
  if [[ -n "$cronline" ]]; then
    say "  offsite (standby): $cronline"
  else
    warn "offsite: no prism-offsite.sh entry in this crontab — install it, or re-run this with sudo to see root's"
  fi

  step "What exists right now"
  report_family() {   # $1 dir  $2 glob  $3 label
    local n newest
    n="$(find "$1" -maxdepth 1 -name "$2" 2>/dev/null | wc -l)"
    newest="$(ls -1t "$1"/$2 2>/dev/null | head -1 || true)"
    if (( n )); then
      say "  $3: $n file(s), newest $(basename "$newest") ($(du -h "$newest" | cut -f1))"
    else
      say "  $3: ${c_ylw}none${c_off}"
    fi
  }
  report_family "$BACKUPS" 'db-*.sql.gz'      "database dumps    "
  report_family "$BACKUPS" 'minio-*.tar.gz'   "document archives "
  report_family "$BACKUPS" 'secrets-*.tar.gz' "secret snapshots  "
  if [[ -n "$vpath" && -d "$vpath" ]]; then
    report_family "$vpath" 'prism-*.sql.gz'  "nightly dumps     "
    report_family "$vpath" 'minio-*.tar.gz'  "nightly documents "
  fi

  step "The restore set to use (newest matched pair)"
  local db minio stamp
  # Newest BY STAMP, not by mtime — a copied or re-synced file carries a fresh mtime,
  # and the stamp in the name is the actual chronology.
  db="$(ls -1 "$BACKUPS"/db-*.sql.gz 2>/dev/null | sort -r | head -1 || true)"
  if [[ -z "$db" ]]; then
    warn "no database dump under $BACKUPS — fall back to the nightly volume above, or the"
    warn "standby box (prism-offsite/deploy + /nightly hold the same files, synced nightly)"
  else
    stamp="$(basename "$db")"; stamp="${stamp#db-}"; stamp="${stamp%.sql.gz}"
    say "  database : $db"
    minio="$BACKUPS/minio-$stamp.tar.gz"
    if [[ -f "$minio" ]]; then
      say "  documents: $minio"
      say "             (same stamp $stamp — a CONSISTENT pair: rows and objects from one moment)"
    else
      minio="$(ls -1 "$BACKUPS"/minio-*.tar.gz 2>/dev/null | sort -r | head -1 || true)"
      if [[ -n "$minio" ]]; then
        warn "no document archive carries stamp $stamp — nearest is $(basename "$minio")."
        warn "documents recorded between the two stamps may not match the rows; prefer a"
        warn "same-stamp pair (any 'backup' or 'upgrade' since the document machinery shipped)"
      else
        warn "NO document archive at all — run 'sudo $0 backup' now to capture one"
      fi
    fi
    if [[ -f "$BACKUPS/secrets-$stamp.tar.gz" ]]; then
      say "  secrets  : $BACKUPS/secrets-$stamp.tar.gz"
    else
      say "  secrets  : $(ls -1 "$BACKUPS"/secrets-*.tar.gz 2>/dev/null | sort -r | head -1 || echo "${c_ylw}none${c_off}")"
    fi
  fi

  step "Which mechanism, for which disaster"
  say "  1. BAD DATA (mistaken import / mass edit — the code is fine):"
  say "       sudo $0 restore-db    <db-STAMP.sql.gz>"
  say "       sudo $0 restore-files <minio-STAMP.tar.gz>      # the SAME stamp"
  say "     Each takes its own safety snapshot first, so the restore is itself reversible."
  say ""
  say "  2. BAD RELEASE (an upgrade went wrong — the data is fine):"
  say "       sudo $0 rollback                # previous code, keeps everything written today"
  say "       sudo $0 rollback --with-db      # …and the pre-upgrade database + documents pair"
  say ""
  say "  3. LOST SERVER (rebuild on a fresh VM from the standby copies):"
  say "       a. install docker + the compose plugin on the new VM"
  say "       b. copy over: the release zip, this script, and the standby box's"
  say "          prism-offsite/deploy/ files (newest db-*, minio-*, secrets-*)"
  say "       c. unzip the release → ./prism ; then:  sudo $0 restore-secrets <secrets-*.tar.gz>"
  say "       d. bring the stack up once (creates the empty volumes), then:"
  say "            sudo $0 restore-db    <newest db-*.sql.gz>"
  say "            sudo $0 restore-files <matching minio-*.tar.gz>"
  say ""
  say "  RPO: at most ONE DAY (nightly sidecars + offsite sync); effectively zero for"
  say "       anything after a manual 'backup'. RTO: 1–2 in minutes, 3 in 30–60 minutes."
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
  ls -1t "$BACKUPS"/minio-*.tar.gz 2>/dev/null | head -3 |
    while read -r f; do say "  $(du -h "$f" | cut -f1)\t$f"; done || true
  step "Data volumes"
  docker volume ls --filter "name=${PROJECT}_" --format '  {{.Name}}' | tee -a "$LOG" >&2 || true
}

cmd_verify() {
  preflight
  if health_once; then say "${c_grn}healthy${c_off}"; else wait_healthy || die "not healthy"; fi
}

# ── entry ────────────────────────────────────────────────────────────────────
# THE DIAGNOSIS MUST SURVIVE THE FAULT IT IS DIAGNOSING. Running as a user who cannot
# write $ROOT means the log file cannot be opened either — and `say` piping into a dead
# `tee` used to kill the script on its first line, so the operator saw a broken pipe
# instead of "you need sudo". Degrade to stderr-only and let preflight do the talking.
mkdir -p "$BACKUPS" 2>/dev/null || true
if ! : >>"$LOG" 2>/dev/null; then
  LOG=/dev/null
  printf '%s\n' "${c_ylw}! cannot write a deploy log under $BACKUPS — continuing to the terminal only${c_off}" >&2
fi
# Checked BEFORE the exec, not around it: a failed redirection on `exec` kills a
# non-interactive shell outright, so `if ! exec 9>…` exits silently — which is how an
# unprivileged run ended with no output at all instead of the one line that helps.
[[ -w "$ROOT" ]] || die "cannot write to $ROOT as $(id -un).
   Run the deploy with sudo:  sudo $0 ${INVOCATION:-upgrade <archive>}
   …or take ownership once:   sudo chown -R $(id -un):$(id -gn) $ROOT"
exec 9>"$LOCK"
flock -n 9 || die "another deploy is running (lock: $LOCK)"
trap 'say "${c_red}✗ aborted at line $LINENO${c_off} — log: $LOG"' ERR

case "${1:-}" in
  upgrade)    shift; cmd_upgrade "$@" ;;
  rollback)   shift; preflight; do_rollback "${1:-}" ;;
  restore-db) shift; cmd_restore_db "$@" ;;
  restore-files) shift; cmd_restore_files "$@" ;;
  restore-secrets) shift; cmd_restore_secrets "$@" ;;
  restore-plan)  cmd_restore_plan ;;
  backup)     preflight; backup_db >/dev/null; backup_files >/dev/null
              backup_secrets >/dev/null; snapshot_images >/dev/null
              say "${c_grn}backup complete${c_off} → $BACKUPS" ;;
  status)     cmd_status ;;
  verify)     cmd_verify ;;
  *) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 1 ;;
esac

#!/usr/bin/env bash
#
# PRISM — offsite backup sync: the production box → the standby box.
#
#   sudo ./prism-offsite.sh install <deploy-dir>   # (re)write the nightly cron for THIS deploy dir, then sync once
#   sudo ./prism-offsite.sh sync    <deploy-dir>   # one sync right now, nothing scheduled
#   sudo ./prism-offsite.sh uninstall              # remove the managed cron entry
#   sudo ./prism-offsite.sh status                 # the cron entry, the last log lines, what the standby holds
#
# The deploy dir moves (aug_11, aug_20, …): `install` REPLACES any previous entry this
# script wrote, so switching is one command — never two entries syncing a dead tree.
# Defaults are overridable through the environment:
#   DEST_HOST=172.31.14.112              DEST_USER=ubuntu
#   DEST_DIR=/home/ubuntu/prism-offsite  SSH_KEY=/home/ubuntu/Prism_stage/prism.pem
#   CRON_TIME="15 2 * * *"               KEEP_REMOTE_DAYS=45   (0 = never prune the standby)
set -euo pipefail

DEST_HOST="${DEST_HOST:-172.31.14.112}"
DEST_USER="${DEST_USER:-ubuntu}"
DEST_DIR="${DEST_DIR:-/home/ubuntu/prism-offsite}"
SSH_KEY="${SSH_KEY:-/home/ubuntu/Prism_stage/prism.pem}"
CRON_TIME="${CRON_TIME:-15 2 * * *}"
KEEP_REMOTE_DAYS="${KEEP_REMOTE_DAYS:-45}"
LOG="${LOG:-/var/log/prism-offsite.log}"
SELF="$(readlink -f "${BASH_SOURCE[0]}")"

say() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }
die() { say "ERROR: $*" >&2; exit 1; }

RSH="ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
_ssh() { $RSH "$DEST_USER@$DEST_HOST" "$@"; }

need_root() {
  [[ "$(id -u)" -eq 0 ]] ||
    die "run with sudo — the docker volume path and root's crontab need it"
}

resolve_dir() {
  local d="${1:-}"
  [[ -n "$d" ]] || die "usage: $0 ${2:-sync} <deploy-dir>   (e.g. /home/ubuntu/aug_20)"
  d="$(readlink -f "$d")"
  # backups/ is what prism-deploy.sh maintains — its absence means the wrong directory,
  # and syncing the wrong directory nightly is worse than failing loudly once.
  [[ -d "$d/backups" ]] || die "$d has no backups/ directory — is that really the deploy dir?"
  echo "$d"
}

# The nightly sidecars (pgbackup + filebackup) write into the *_pgbackups volume —
# found by suffix so the compose project name never has to be configured here.
volume_path() {
  local vol
  vol="$(docker volume ls -q 2>/dev/null | grep -E '_pgbackups$' | head -1 || true)"
  [[ -n "$vol" ]] || return 0
  docker volume inspect -f '{{.Mountpoint}}' "$vol" 2>/dev/null || true
}

cmd_sync() {
  local dir; dir="$(resolve_dir "${1:-}" sync)"
  [[ -r "$SSH_KEY" ]] || die "cannot read $SSH_KEY"
  _ssh "mkdir -p '$DEST_DIR/deploy' '$DEST_DIR/nightly'"

  say "sync: $dir/backups/ → $DEST_USER@$DEST_HOST:$DEST_DIR/deploy/"
  rsync -az -e "$RSH" "$dir/backups/" "$DEST_USER@$DEST_HOST:$DEST_DIR/deploy/"

  local vpath; vpath="$(volume_path)"
  if [[ -n "$vpath" && -d "$vpath" ]]; then
    say "sync: $vpath/ → $DEST_DIR/nightly/"
    rsync -az -e "$RSH" "$vpath/" "$DEST_USER@$DEST_HOST:$DEST_DIR/nightly/"
  else
    say "note: no *_pgbackups volume found — the nightly sidecar archives were skipped"
  fi

  # No --delete anywhere: the standby only accumulates. Age is the only pruner, and
  # only when asked (KEEP_REMOTE_DAYS=0 keeps everything forever). Logs and image
  # manifests age out on the same clock as the archives they accompanied.
  if (( KEEP_REMOTE_DAYS > 0 )); then
    _ssh "find '$DEST_DIR' \( -name '*.gz' -o -name '*.log' -o -name '*.tsv' \) -mtime +$KEEP_REMOTE_DAYS -delete" || true
  fi

  # The sidecars stamp a marker when a nightly archive failed verification —
  # surface it HERE so the courier's own log carries the warning every night
  # until the chain is healthy again.
  if [[ -n "$vpath" ]]; then
    local m
    for m in "$vpath/PGBACKUP_FAILED" "$vpath/FILEBACKUP_FAILED"; do
      [[ -f "$m" ]] && say "WARNING: $(basename "$m") — $(tail -1 "$m" 2>/dev/null) — the latest nightly archive is MISSING, investigate the sidecar"
    done
  fi
  say "sync complete"
}

cmd_install() {
  need_root
  local dir; dir="$(resolve_dir "${1:-}" install)"
  local line="$CRON_TIME $SELF sync $dir >> $LOG 2>&1"
  local current; current="$(crontab -l 2>/dev/null || true)"
  # Every line naming this script is ours — drop them all, append the fresh one.
  { printf '%s\n' "$current" | grep -vF "$SELF" || true; printf '%s\n' "$line"; } | crontab -
  say "cron installed (any previous entry replaced):"
  say "  $line"
  say "running the first sync now…"
  cmd_sync "$dir"
}

cmd_uninstall() {
  need_root
  local current; current="$(crontab -l 2>/dev/null || true)"
  { printf '%s\n' "$current" | grep -vF "$SELF" || true; } | crontab -
  say "managed cron entry removed"
}

cmd_status() {
  say "managed cron entry (run with sudo to see root's crontab):"
  crontab -l 2>/dev/null | grep -F "$SELF" || echo "  (none installed)"
  local vpath; vpath="$(volume_path)"
  if [[ -n "$vpath" ]]; then
    local m; local any=0
    for m in "$vpath/PGBACKUP_FAILED" "$vpath/FILEBACKUP_FAILED"; do
      [[ -f "$m" ]] && { say "WARNING: $(basename "$m") — $(tail -1 "$m" 2>/dev/null)"; any=1; }
    done
    (( any )) || say "nightly sidecars: healthy (no failure markers)"
  fi
  say "last log lines ($LOG):"
  tail -5 "$LOG" 2>/dev/null || echo "  (no log yet)"
  say "the standby holds (newest first):"
  _ssh "ls -lt '$DEST_DIR/deploy' 2>/dev/null | head -6; echo; ls -lt '$DEST_DIR/nightly' 2>/dev/null | head -6" ||
    echo "  (standby unreachable)"
}

case "${1:-}" in
  sync)      shift; cmd_sync "$@" ;;
  install)   shift; cmd_install "$@" ;;
  uninstall) cmd_uninstall ;;
  status)    cmd_status ;;
  *) sed -n "2,15p" "$SELF" | sed 's/^# \?//'; exit 1 ;;
esac

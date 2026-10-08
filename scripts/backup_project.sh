#!/usr/bin/env bash
# Daily snapshot of the whole Open WebUI docker project folder, kept locally and copied to another host.
#
#   1. stop the compose stack (so SQLite and the vector DB are quiet and consistent),
#   2. rsync the project folder to a dated snapshot directory; unchanged files are hard links to the
#      previous snapshot (--link-dest), so every day looks like a full copy but only changes take space,
#   3. start the stack again (also on any error - the trap below), prune local snapshots,
#   4. rsync that snapshot to the remote host the same way, then prune the remote snapshots.
#
# Must run as root: the project folder holds root- and uid-1001-owned files (open-terminal, data/) that
# gordon can't read, and ownership has to be recorded to restore properly. The remote user isn't root,
# so ownership there is kept in extended attributes (--fake-super) - see "Restore" in the README.
#
# Cron example (root's crontab, `sudo crontab -e`; 06:15 daily, after the hourly autonomy tick):
#   15 6 * * * /home/gordon/dev/open-webui-tools/scripts/backup_project.sh >> /home/gordon/backup/backup_project.log 2>&1
#
# Usage:  backup_project.sh [--dry-run] [--no-remote]
#   --dry-run    don't stop the stack; rsync -n against the real folder (checks it is all readable); no
#                prune, no remote copy. Safe to run any time.
#   --no-remote  take the local snapshot only.
#
# Override via environment: PROJECT_DIR, BACKUP_DIR, KEEP_LOCAL, KEEP_REMOTE, REMOTE_HOST, REMOTE_DIR,
# SSH_KEY, SSH_CONFIG, EXCLUDES (space-separated rsync patterns, relative to PROJECT_DIR).
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/gordon/docker/open-webui}"
BACKUP_DIR="${BACKUP_DIR:-/home/gordon/backup/openwebui}"   # must be OUTSIDE PROJECT_DIR
KEEP_LOCAL="${KEEP_LOCAL:-7}"
KEEP_REMOTE="${KEEP_REMOTE:-30}"
REMOTE_HOST="${REMOTE_HOST:-gordon@docker-server}"           # user@host; root's ssh would default to root
REMOTE_DIR="${REMOTE_DIR:-/home/gordon/backup/openwebui}"
SSH_KEY="${SSH_KEY:-/home/gordon/.ssh/id_rsa}"
SSH_CONFIG="${SSH_CONFIG:-/home/gordon/.ssh/config}"
# data/cache is 3+ GB of re-downloadable embedding/whisper models.
EXCLUDES="${EXCLUDES:-data/cache/}"

DRY_RUN=0
REMOTE=1
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --no-remote) REMOTE=0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

log() { echo "$(date -Is) $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || [ "${ALLOW_NON_ROOT:-0}" = 1 ] || die "must run as root (sudo); the project folder has root-owned files"
[ -d "$PROJECT_DIR" ] || die "project folder $PROJECT_DIR not found"
case "$(realpath -m "$BACKUP_DIR")/" in
  "$(realpath -m "$PROJECT_DIR")"/*) die "BACKUP_DIR must be outside PROJECT_DIR" ;;
esac
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"   # snapshots contain .env (API keys)

exec 9>"$BACKUP_DIR/.lock"
flock -n 9 || die "another backup is already running"

SNAP_RE='^[0-9]{4}-[0-9]{2}-[0-9]{2}(_[0-9]{4})?$'
stamp="$(date +%Y-%m-%d)"
[ -e "$BACKUP_DIR/$stamp" ] && stamp="${stamp}_$(date +%H%M)"
partial="$BACKUP_DIR/$stamp.partial"
prev="$(ls -1 "$BACKUP_DIR" | grep -E "$SNAP_RE" | tail -n 1 || true)"

excludes=()
for pat in $EXCLUDES; do excludes+=(--exclude="$pat"); done

# ---- stack control: always bring the stack back up, even if rsync fails ----
stack_stopped=0
start_stack() {
  if [ "$stack_stopped" -eq 1 ]; then
    log "starting stack"
    (cd "$PROJECT_DIR" && docker compose start) && stack_stopped=0 || log "ERROR: docker compose start failed - start it by hand" >&2
  fi
}
trap start_stack EXIT

if [ "$DRY_RUN" -eq 0 ]; then
  log "stopping stack"
  (cd "$PROJECT_DIR" && docker compose stop)
  stack_stopped=1
else
  log "dry run: leaving the stack running; rsync -n only"
fi

# ---- local snapshot ----
rsync_opts=(-aHAX --numeric-ids --delete "${excludes[@]}")
[ -n "$prev" ] && rsync_opts+=(--link-dest="$BACKUP_DIR/$prev")
if [ "$DRY_RUN" -eq 1 ]; then
  log "dry run: would write $partial (link-dest ${prev:-none})"
  rsync -n --stats "${rsync_opts[@]}" "$PROJECT_DIR/" "$partial/" | grep -E 'Number of files|Total file size|Total transferred' || true
  exit 0
fi
log "snapshot -> $BACKUP_DIR/$stamp (link-dest ${prev:-none})"
rm -rf "$partial"
rsync "${rsync_opts[@]}" "$PROJECT_DIR/" "$partial/"
mv "$partial" "$BACKUP_DIR/$stamp"
log "local snapshot done ($(du -sh "$BACKUP_DIR/$stamp" | cut -f1) apparent size; unchanged files are hard links)"

start_stack   # the remote copy doesn't need the stack stopped

# ---- prune local ----
ls -1 "$BACKUP_DIR" | grep -E "$SNAP_RE" | head -n -"$KEEP_LOCAL" | while read -r old; do
  log "pruning local snapshot $old"
  rm -rf "${BACKUP_DIR:?}/$old"
done || true

# ---- remote copy (a failure here leaves the good local snapshot in place) ----
if [ "$REMOTE" -eq 0 ]; then
  log "skipping remote copy (--no-remote)"
  exit 0
fi
ssh_cmd=(ssh -F "$SSH_CONFIG" -i "$SSH_KEY" -o BatchMode=yes -o UserKnownHostsFile=/home/gordon/.ssh/known_hosts)
rssh="${ssh_cmd[*]}"
remote_prev="$("${ssh_cmd[@]}" "$REMOTE_HOST" "mkdir -p '$REMOTE_DIR' && chmod 700 '$REMOTE_DIR' && ls -1 '$REMOTE_DIR'" | grep -E "$SNAP_RE" | tail -n 1 || true)"
remote_opts=(-aHAX --numeric-ids --delete -e "$rssh" --rsync-path="rsync --fake-super")
[ -n "$remote_prev" ] && remote_opts+=(--link-dest="$REMOTE_DIR/$remote_prev")
log "remote copy -> $REMOTE_HOST:$REMOTE_DIR/$stamp (link-dest ${remote_prev:-none})"
rsync "${remote_opts[@]}" "$BACKUP_DIR/$stamp/" "$REMOTE_HOST:$REMOTE_DIR/$stamp.partial/" \
  || die "remote copy failed; the local snapshot $stamp is intact"
"${ssh_cmd[@]}" "$REMOTE_HOST" "mv '$REMOTE_DIR/$stamp.partial' '$REMOTE_DIR/$stamp'" \
  || die "could not finalize remote snapshot"

# ---- prune remote ----
"${ssh_cmd[@]}" "$REMOTE_HOST" "ls -1 '$REMOTE_DIR'" | grep -E "$SNAP_RE" | head -n -"$KEEP_REMOTE" | while read -r old; do
  log "pruning remote snapshot $old"
  "${ssh_cmd[@]}" "$REMOTE_HOST" "rm -rf '${REMOTE_DIR:?}/$old'"
done || true
log "done"

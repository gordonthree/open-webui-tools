#!/usr/bin/env bash
# Timestamped .tgz backup of the companion-docs folder, with simple retention.
# Cron example (daily at 03:15):
#   15 3 * * * /home/gordon/docker/open-webui/backup_companion.sh >> /home/gordon/docker/open-webui/backups/backup.log 2>&1
#
# Override via environment: SRC_DIR, BACKUP_DIR, KEEP_DAYS
set -euo pipefail

SRC_DIR="${SRC_DIR:-/home/gordon/docker/open-webui/companion-docs}"
BACKUP_DIR="${BACKUP_DIR:-/home/gordon/docker/open-webui/backups}"
KEEP_DAYS="${KEEP_DAYS:-30}"

[ -d "$SRC_DIR" ] || { echo "$(date -Is) ERROR: source $SRC_DIR not found" >&2; exit 1; }
mkdir -p "$BACKUP_DIR"

name="$(basename "$SRC_DIR")"
out="$BACKUP_DIR/${name}-$(date +%Y%m%d-%H%M%S).tgz"

tar -czf "$out" -C "$(dirname "$SRC_DIR")" "$name"
echo "$(date -Is) wrote $out ($(du -h "$out" | cut -f1))"

# prune old backups
find "$BACKUP_DIR" -maxdepth 1 -name "${name}-*.tgz" -mtime +"$KEEP_DAYS" -print -delete

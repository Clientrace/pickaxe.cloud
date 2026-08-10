#!/usr/bin/env bash
# Snapshot the world to S3. Safe to run while players are online.
set -euo pipefail

set -a
# shellcheck disable=SC1091
source /etc/pickaxe/pickaxe.env
set +a
export AWS_DEFAULT_REGION="$PICKAXE_REGION"

MC_DIR=/opt/minecraft
RCON=/usr/local/bin/pickaxe-rcon
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
KEY="backups/world-$STAMP.tar.gz"
ARCHIVE=$(mktemp /tmp/pickaxe-backup-XXXXXX.tar.gz)

running() { systemctl is-active --quiet minecraft.service; }

cleanup() {
  rm -f "$ARCHIVE"
  # Never leave autosave off, even if the archive step blew up.
  running && $RCON "save-on" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if running; then
  $RCON "save-off" >/dev/null || true
  $RCON "save-all flush" >/dev/null || true
  sleep 3
fi

# server.jar and the vanilla caches are excluded: they are re-downloadable, and
# leaving them out keeps a backup to roughly the size of the world itself.
#
# tar exits 1 for "file changed as we read it", which is routine on a live
# server -- the JVM keeps touching session.lock and player data even with
# autosave off. Only exit >= 2 is a genuine failure.
set +e
tar -czf "$ARCHIVE" -C "$MC_DIR" \
  --warning=no-file-changed \
  --warning=no-file-removed \
  --exclude=./logs \
  --exclude=./cache \
  --exclude=./libraries \
  --exclude=./versions \
  --exclude=./server.jar \
  --exclude=./jvm.args \
  --exclude=./session.lock \
  .
TAR_STATUS=$?
set -e

if [ "$TAR_STATUS" -eq 1 ]; then
  echo "note: some files changed while archiving (normal on a running server)"
elif [ "$TAR_STATUS" -ne 0 ]; then
  echo "ERROR: tar failed with exit status $TAR_STATUS" >&2
  exit "$TAR_STATUS"
fi

if running; then
  $RCON "save-on" >/dev/null || true
fi

aws s3 cp "$ARCHIVE" "s3://$PICKAXE_BUCKET/$KEY" --only-show-errors
echo "backed up to s3://$PICKAXE_BUCKET/$KEY ($(du -h "$ARCHIVE" | cut -f1))"

# --------------------------------------------------------------- retention
KEYS=$(aws s3api list-objects-v2 \
  --bucket "$PICKAXE_BUCKET" --prefix backups/ \
  --query 'sort_by(Contents, &LastModified)[].Key' --output text 2>/dev/null || echo "")

if [ -n "$KEYS" ] && [ "$KEYS" != "None" ]; then
  echo "$KEYS" | tr '\t' '\n' | grep -v '^$' | head -n "-$PICKAXE_BACKUP_KEEP" |
    while read -r stale; do
      echo "pruning s3://$PICKAXE_BUCKET/$stale"
      aws s3 rm "s3://$PICKAXE_BUCKET/$stale" --only-show-errors
    done
fi

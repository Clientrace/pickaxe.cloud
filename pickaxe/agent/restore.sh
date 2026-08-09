#!/usr/bin/env bash
# Restore a world from S3. Usage: restore.sh <backups/world-....tar.gz|latest>
set -euo pipefail

set -a
# shellcheck disable=SC1091
source /etc/pickaxe/pickaxe.env
set +a
export AWS_DEFAULT_REGION="$PICKAXE_REGION"

MC_DIR=/opt/minecraft
TARGET="${1:-latest}"

if [ "$TARGET" = "latest" ]; then
  TARGET=$(aws s3api list-objects-v2 \
    --bucket "$PICKAXE_BUCKET" --prefix backups/ \
    --query 'sort_by(Contents, &LastModified)[-1].Key' --output text)
  if [ -z "$TARGET" ] || [ "$TARGET" = "None" ]; then
    echo "ERROR: no backups found in s3://$PICKAXE_BUCKET/backups/" >&2
    exit 1
  fi
fi

echo "--> restoring $TARGET"
ARCHIVE=$(mktemp /tmp/pickaxe-restore-XXXXXX.tar.gz)
trap 'rm -f "$ARCHIVE"' EXIT
aws s3 cp "s3://$PICKAXE_BUCKET/$TARGET" "$ARCHIVE" --only-show-errors

if systemctl is-active --quiet minecraft.service; then
  echo "--> stopping Minecraft"
  systemctl stop minecraft.service
fi

# Keep the world we are replacing until the next restore, in case this was a
# mistake -- an unrecoverable `rm -rf` on someone's world is not acceptable.
ROLLBACK="$MC_DIR/.pickaxe-pre-restore"
rm -rf "$ROLLBACK"
install -d -o minecraft -g minecraft "$ROLLBACK"
shopt -s nullglob
for entry in "$MC_DIR"/world*; do
  mv "$entry" "$ROLLBACK"/
done
shopt -u nullglob

tar -xzf "$ARCHIVE" -C "$MC_DIR"
chown -R minecraft:minecraft "$MC_DIR"
echo "--> restored; previous world kept at $ROLLBACK"

# The archive carries its own server.properties, which may hold a stale RCON
# password. Re-running install.sh reasserts the managed settings and starts up.
/opt/pickaxe/install.sh

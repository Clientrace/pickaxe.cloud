#!/usr/bin/env bash
# Stop the EC2 instance once the server has been empty long enough.
# Driven by pickaxe-idle.timer (every 2 minutes).
set -euo pipefail

set -a
# shellcheck disable=SC1091
source /etc/pickaxe/pickaxe.env
set +a
export AWS_DEFAULT_REGION="$PICKAXE_REGION"

STATE=/var/lib/pickaxe
LAST_ACTIVE="$STATE/last-active"
RCON=/usr/local/bin/pickaxe-rcon

log() { logger -t pickaxe-idle "$*"; }

# Give players a window to join after a wake-up before we consider sleeping.
UPTIME=$(cut -d. -f1 /proc/uptime)
if [ "$UPTIME" -lt $((PICKAXE_BOOT_GRACE * 60)) ]; then
  exit 0
fi

if ! systemctl is-active --quiet minecraft.service; then
  exit 0
fi

# If the console is unreachable the server is probably mid-start or wedged.
# Either way we cannot prove it is empty, so do nothing.
REPLY=$($RCON list 2>/dev/null || true)
PLAYERS=$(grep -oP 'There are \K[0-9]+' <<<"$REPLY" || true)
if [ -z "$PLAYERS" ]; then
  exit 0
fi

NOW=$(date +%s)
if [ "$PLAYERS" -gt 0 ]; then
  echo "$NOW" >"$LAST_ACTIVE"
  exit 0
fi

# The marker outlives a sleep/wake cycle, so a stale value from before the last
# shutdown would make a freshly woken server look like it had been empty for
# hours and stop again the moment the boot grace expired. Anything older than
# this boot means "empty since boot", not "empty since then".
BOOT=$((NOW - UPTIME))
LAST=$(cat "$LAST_ACTIVE" 2>/dev/null || echo 0)
if [ -z "$LAST" ] || [ "$LAST" -lt "$BOOT" ]; then
  LAST=$BOOT
  echo "$LAST" >"$LAST_ACTIVE"
fi

IDLE=$((NOW - LAST))
if [ "$IDLE" -lt $((PICKAXE_IDLE_MINUTES * 60)) ]; then
  exit 0
fi

log "empty for ${IDLE}s, shutting down"
$RCON "say Server has been empty for ${PICKAXE_IDLE_MINUTES} minutes - going to sleep." >/dev/null 2>&1 || true
sleep 5

systemctl stop minecraft.service || true
"$(dirname "$0")/backup.sh" || log "final backup failed, stopping anyway"

TOKEN=$(curl -fsS -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
INSTANCE_ID=$(curl -fsS -H "X-aws-ec2-metadata-token: $TOKEN" \
  "http://169.254.169.254/latest/meta-data/instance-id")

log "stopping instance $INSTANCE_ID"
aws ec2 stop-instances --instance-ids "$INSTANCE_ID" >/dev/null

#!/usr/bin/env bash
# Pull the current config + agent from S3 and apply them, then hand off to
# install.sh. Runs at every boot (pickaxe-boot.service), which is what makes
# `pickaxe up` on a sleeping server take effect the next time it wakes.
set -euo pipefail

exec > >(tee -a /var/log/pickaxe-sync.log) 2>&1
echo "=== pickaxe sync $(date -u +%FT%TZ) ==="

set -a
# shellcheck disable=SC1091
source /etc/pickaxe/pickaxe.env
set +a
export AWS_DEFAULT_REGION="$PICKAXE_REGION"

BUCKET="$PICKAXE_BUCKET"

# If S3 is unreachable, carry on with whatever is already on disk rather than
# leaving the server down.
if aws s3 cp "s3://$BUCKET/bootstrap/pickaxe.env" /tmp/pickaxe.env --only-show-errors; then
  mv /tmp/pickaxe.env /etc/pickaxe/pickaxe.env
else
  echo "WARN: could not fetch pickaxe.env, using the local copy" >&2
fi

if aws s3 cp "s3://$BUCKET/bootstrap/agent.tar.gz" /tmp/agent.tar.gz --only-show-errors; then
  tar -xzf /tmp/agent.tar.gz -C /opt/pickaxe
  chmod +x /opt/pickaxe/*.sh
  rm -f /tmp/agent.tar.gz
else
  echo "WARN: could not fetch agent.tar.gz, using the local copy" >&2
fi

exec /opt/pickaxe/install.sh

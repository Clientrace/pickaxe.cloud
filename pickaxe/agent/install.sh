#!/usr/bin/env bash
# Provisions (and re-provisions) the Minecraft server on the instance.
#
# Runs on first boot from UserData, and again from `pickaxe up` via SSM whenever
# config.yaml changes. Must be idempotent: it is expected to run repeatedly on a
# live server with a world on disk.
set -euo pipefail

exec > >(tee -a /var/log/pickaxe-install.log) 2>&1
echo "=== pickaxe install $(date -u +%FT%TZ) ==="

MC_DIR=/opt/minecraft
PX_DIR=/opt/pickaxe
STATE=/var/lib/pickaxe

set -a
# shellcheck disable=SC1091
source /etc/pickaxe/pickaxe.env
set +a
export AWS_DEFAULT_REGION="$PICKAXE_REGION"

install -d "$STATE"

# --------------------------------------------------------------- packages
export DEBIAN_FRONTEND=noninteractive

APT_UPDATED=false
apt_update_once() {
  if [ "$APT_UPDATED" = true ]; then return 0; fi
  for attempt in 1 2 3; do
    if apt-get update -y; then APT_UPDATED=true; return 0; fi
    sleep 15
  done
  return 1
}

if ! command -v jq >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
  apt_update_once || true
  apt-get install -y --no-install-recommends curl jq unzip tar ca-certificates python3
fi

# The Java version Minecraft needs climbs over time -- 1.21 wanted Java 21,
# newer releases want 25, and running too old a JVM fails with
# UnsupportedClassVersionError before the server ever binds a port. Java is
# backward compatible, so install the newest JRE this release offers.
apt_update_once || true
JAVA_PKG=""
for major in 25 24 23 22 21; do
  if apt-cache show "openjdk-${major}-jre-headless" >/dev/null 2>&1; then
    JAVA_PKG="openjdk-${major}-jre-headless"
    break
  fi
done
if [ -z "$JAVA_PKG" ]; then
  echo "ERROR: no openjdk JRE package available on this system" >&2
  exit 1
fi
if ! dpkg -s "$JAVA_PKG" >/dev/null 2>&1; then
  echo "--> installing $JAVA_PKG"
  apt-get install -y --no-install-recommends "$JAVA_PKG"
fi

# Several JDKs can coexist and /usr/bin/java may still point at an older one,
# so always launch through the chosen package's own binary.
JAVA_BIN=$(dpkg -L "$JAVA_PKG" | grep -m1 -E '^/usr/lib/jvm/[^/]+/bin/java$' || echo /usr/bin/java)
echo "--> java: $JAVA_BIN ($("$JAVA_BIN" -version 2>&1 | head -1))"

# --------------------------------------------------------------- user & dirs
if ! id -u minecraft >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$MC_DIR" --shell /usr/sbin/nologin minecraft
fi
install -d -o minecraft -g minecraft "$MC_DIR"

# --------------------------------------------------------------- world seed
# Only on a genuinely fresh box; never clobber a world that already exists.
if [ ! -e "$MC_DIR/.pickaxe-provisioned" ]; then
  if aws s3 ls "s3://$PICKAXE_BUCKET/seed/serverdata.tar.gz" >/dev/null 2>&1; then
    echo "--> seeding world from s3://$PICKAXE_BUCKET/seed/serverdata.tar.gz"
    aws s3 cp "s3://$PICKAXE_BUCKET/seed/serverdata.tar.gz" /tmp/seed.tar.gz
    tar -xzf /tmp/seed.tar.gz -C "$MC_DIR"
    rm -f /tmp/seed.tar.gz
  else
    echo "--> no seed found, a fresh world will be generated"
  fi
  touch "$MC_DIR/.pickaxe-provisioned"
fi

# --------------------------------------------------------------- server jar
# `version: keep` leaves whatever jar was seeded (Paper, Fabric, Forge...) alone.
CURRENT_VERSION=$(cat "$MC_DIR/.mc-version" 2>/dev/null || echo "")
if [ "$PICKAXE_MC_VERSION" = "keep" ] && [ -f "$MC_DIR/server.jar" ]; then
  echo "--> keeping existing server.jar"
elif [ "$PICKAXE_MC_VERSION" = "keep" ]; then
  echo "ERROR: minecraft.version is 'keep', but there is no server.jar." >&2
  echo "       'keep' means 'use the jar I supplied' -- put your Paper/Fabric/Forge" >&2
  echo "       jar in the folder that minecraft.local_server_path points at, named" >&2
  echo "       server.jar, then run 'pickaxe up'. For a vanilla server, set" >&2
  echo "       minecraft.version to 'latest' or a version such as '1.21.4' instead." >&2
  exit 1
elif [ ! -f "$MC_DIR/server.jar" ] || [ "$PICKAXE_MC_VERSION" != "$CURRENT_VERSION" ]; then
  MANIFEST=$(curl -fsSL https://launchermeta.mojang.com/mc/game/version_manifest_v2.json)
  WANT="$PICKAXE_MC_VERSION"
  if [ "$WANT" = "latest" ]; then
    WANT=$(jq -r '.latest.release' <<<"$MANIFEST")
  fi
  VERSION_URL=$(jq -r --arg v "$WANT" '.versions[] | select(.id == $v) | .url' <<<"$MANIFEST")
  if [ -z "$VERSION_URL" ] || [ "$VERSION_URL" = "null" ]; then
    echo "ERROR: unknown Minecraft version '$WANT'" >&2
    exit 1
  fi
  JAR_URL=$(curl -fsSL "$VERSION_URL" | jq -r '.downloads.server.url')
  echo "--> downloading Minecraft $WANT"
  curl -fsSL "$JAR_URL" -o "$MC_DIR/server.jar.new"
  mv "$MC_DIR/server.jar.new" "$MC_DIR/server.jar"
  echo "$WANT" >"$MC_DIR/.mc-version"
fi

# --------------------------------------------------------------- eula & rcon secret
echo "eula=true" >"$MC_DIR/eula.txt"

if [ ! -s "$STATE/rcon.pass" ]; then
  head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 24 >"$STATE/rcon.pass"
fi
# Readable by the minecraft user so systemd's ExecStop can reach the console.
chown root:minecraft "$STATE/rcon.pass"
chmod 640 "$STATE/rcon.pass"
RCON_PASS=$(cat "$STATE/rcon.pass")

# --------------------------------------------------------------- server.properties
# RCON is bound on all interfaces by the server, but 25575 is not in the
# security group -- the only way in is from the instance itself (or SSM).
touch "$MC_DIR/server.properties"
python3 "$PX_DIR/props.py" "$MC_DIR/server.properties" \
  "server-port=$PICKAXE_PORT" \
  "motd=$PICKAXE_MOTD" \
  "enable-rcon=true" \
  "rcon.port=25575" \
  "rcon.password=$RCON_PASS" \
  "broadcast-rcon-to-ops=false"

# --------------------------------------------------------------- JVM tuning
# Aikar's flags: measurably better pause times than stock G1 settings.
cat >"$MC_DIR/jvm.args" <<EOF
-Xms${PICKAXE_RAM_GB}G
-Xmx${PICKAXE_RAM_GB}G
-XX:+UseG1GC
-XX:+ParallelRefProcEnabled
-XX:MaxGCPauseMillis=200
-XX:+UnlockExperimentalVMOptions
-XX:+DisableExplicitGC
-XX:+AlwaysPreTouch
-XX:G1NewSizePercent=30
-XX:G1MaxNewSizePercent=40
-XX:G1HeapRegionSize=8M
-XX:G1ReservePercent=20
-XX:G1HeapWastePercent=5
-XX:G1MixedGCCountTarget=4
-XX:InitiatingHeapOccupancyPercent=15
-XX:G1MixedGCLiveThresholdPercent=90
-XX:G1RSetUpdatingPauseTimePercent=5
-XX:SurvivorRatio=32
-XX:+PerfDisableSharedMem
-XX:MaxTenuringThreshold=1
-Dusing.aikars.flags=https://mcflags.emc.gs
-Daikars.new.flags=true
EOF

chown -R minecraft:minecraft "$MC_DIR"

# --------------------------------------------------------------- helper binary
cat >/usr/local/bin/pickaxe-rcon <<'EOF'
#!/usr/bin/env bash
exec /usr/bin/python3 /opt/pickaxe/mcrcon.py "$@"
EOF
chmod 755 /usr/local/bin/pickaxe-rcon

# --------------------------------------------------------------- systemd units
cat >/etc/systemd/system/pickaxe-boot.service <<EOF
[Unit]
Description=Pull pickaxe configuration from S3 and apply it
After=network-online.target
Wants=network-online.target
Before=minecraft.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$PX_DIR/sync.sh
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/minecraft.service <<EOF
[Unit]
Description=Minecraft server (pickaxe/$PICKAXE_SERVER)
After=network-online.target pickaxe-boot.service
Wants=network-online.target pickaxe-boot.service
# A misconfiguration can crash-loop for a long time; never let systemd give up
# permanently, or a later fix would need a manual reset-failed.
StartLimitIntervalSec=0

[Service]
Type=simple
User=minecraft
Group=minecraft
WorkingDirectory=$MC_DIR
ExecStart=$JAVA_BIN @$MC_DIR/jvm.args -jar $MC_DIR/server.jar nogui
# Graceful shutdown: "stop" flushes chunks to disk. SIGTERM does not.
ExecStop=/usr/local/bin/pickaxe-rcon stop
TimeoutStopSec=180
Restart=on-failure
RestartSec=15
SuccessExitStatus=0 143

[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/pickaxe-backup.service <<EOF
[Unit]
Description=Back up the Minecraft world to S3

[Service]
Type=oneshot
ExecStart=$PX_DIR/backup.sh
EOF

cat >/etc/systemd/system/pickaxe-backup.timer <<EOF
[Unit]
Description=Periodic Minecraft world backup

[Timer]
OnBootSec=10min
OnUnitActiveSec=${PICKAXE_BACKUP_INTERVAL}min
Persistent=false

[Install]
WantedBy=timers.target
EOF

cat >/etc/systemd/system/pickaxe-idle.service <<EOF
[Unit]
Description=Stop the instance when nobody is playing

[Service]
Type=oneshot
ExecStart=$PX_DIR/idle.sh
EOF

cat >/etc/systemd/system/pickaxe-idle.timer <<EOF
[Unit]
Description=Idle check for the Minecraft server

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min
Persistent=false

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload

# --------------------------------------------------------------- start / restart
# Restart only when something the running JVM actually cares about changed,
# so a no-op `pickaxe up` never kicks players.
FINGERPRINT=$( { cat "$MC_DIR/jvm.args" "$MC_DIR/server.properties" "$MC_DIR/.mc-version" 2>/dev/null; echo "$JAVA_BIN"; } | sha256sum | cut -d' ' -f1)
PREVIOUS=$(cat "$STATE/fingerprint" 2>/dev/null || echo "")

systemctl enable pickaxe-boot.service >/dev/null
systemctl enable minecraft.service >/dev/null

# --no-block matters: at boot this script runs *inside* pickaxe-boot.service,
# and minecraft.service is ordered After= it. A blocking start would deadlock.
if ! systemctl is-active --quiet minecraft.service; then
  echo "--> starting Minecraft"
  systemctl start --no-block minecraft.service
elif [ "$FINGERPRINT" != "$PREVIOUS" ]; then
  echo "--> configuration changed, restarting Minecraft"
  systemctl restart --no-block minecraft.service
else
  echo "--> Minecraft already running with current configuration"
fi
echo "$FINGERPRINT" >"$STATE/fingerprint"

systemctl enable --now pickaxe-backup.timer >/dev/null

if [ "$PICKAXE_IDLE_ENABLED" = "true" ]; then
  systemctl enable --now pickaxe-idle.timer >/dev/null
  echo "--> idle shutdown after ${PICKAXE_IDLE_MINUTES}m empty"
else
  systemctl disable --now pickaxe-idle.timer >/dev/null 2>&1 || true
  echo "--> idle shutdown disabled"
fi

echo "=== pickaxe install done $(date -u +%FT%TZ) ==="

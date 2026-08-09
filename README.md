<p align="center">
  <img src="media/pickaxe.cloud.png" alt="pickaxe.cloud" width="480">
</p>

<p align="center"><b>Minecraft servers on AWS that you can put to sleep.</b></p>

One config file, one command to create the server, and two commands — `wake` and
`sleep` — to control what you pay for. The server also puts *itself* to sleep
when nobody has been online for a while, so forgetting to shut it down doesn't
cost you a month of compute.

```
pickaxe init     # answer a few questions -> config.yaml
pickaxe up       # build it on AWS
pickaxe sleep    # stop paying for compute
pickaxe wake     # bring it back, same IP, same world
```

---

## Start here

### 1. Prerequisites

| Thing | Why | Check |
| --- | --- | --- |
| Python 3.10+ | the CLI | `python3 --version` |
| Node.js 18+ and npm | Serverless Framework provisions the AWS resources | `node --version` |
| An AWS account with credentials configured | everything | `aws sts get-caller-identity` |

If that last command fails, install the [AWS CLI](https://aws.amazon.com/cli/)
and run `aws configure`. You'll need an access key with permission to create
EC2, IAM, S3 and CloudFormation resources.

> The Serverless Framework itself is installed automatically on first use — a
> pinned copy of v3 lands in `.pickaxe/deploy/`. You never install it globally,
> and there's no Serverless account or login involved.

### 2. Install the CLI

```bash
git clone <this repo> pickaxe.cloud
cd pickaxe.cloud

python3 -m venv .venv
source .venv/bin/activate          # fish: source .venv/bin/activate.fish
pip install -e .

pickaxe --help
```

### 3. Create your config

```bash
pickaxe init
```

It asks for a server name, region, instance size, Minecraft version, and
whether you have an existing world to bring across. Everything has a default —
pressing enter the whole way gives you a working 5-player server. The answers
land in `config.yaml`, which you can edit by hand afterwards.

### 4. Bring the server up

```bash
pickaxe up
```

First run takes 4–6 minutes: it creates the AWS resources, boots an instance,
installs Java, downloads the server jar, and (if you pointed it at one) restores
your world. When it's done you get an address to hand to your friends:

```
╭────────── survival ──────────╮
│ Server address               │
│ 52.90.14.203                 │
│                              │
│ pickaxe sleep  stop paying…  │
│ pickaxe wake   bring it back │
╰──────────────────────────────╯
```

That address is an Elastic IP — it does **not** change when you sleep and wake
the server, so nobody has to update their server list.

---

## Bringing an existing world across

Point `minecraft.local_server_path` at the folder that has your `world/`
directory in it:

```yaml
minecraft:
  local_server_path: ./world_data
```

```text
world_data/
├── world/              # and world_nether/, world_the_end/
├── server.properties
├── ops.json
├── whitelist.json
└── plugins/            # or mods/, if you use them
```

`pickaxe up` packages that folder, uploads it to S3, and the instance unpacks it
on first boot. Logs, caches and `server.jar` are skipped — they're regenerated
or re-downloaded.

Seeding only happens **once**, on a genuinely empty server, so a later
`pickaxe up` can never silently overwrite the live world. To push your local
copy over a server that's already running, use `pickaxe up --reseed` — it asks
for confirmation first, and keeps the world it replaces at
`/opt/minecraft/.pickaxe-pre-restore` on the instance.

Running a modded jar (Paper, Fabric, Forge)? Put it in the folder as
`server.jar` and set `minecraft.version: keep` so Pickaxe leaves it alone.

---

## The commands

### Everyday

| Command | What it does |
| --- | --- |
| `pickaxe up` | Create or update the server. Safe to run repeatedly. |
| `pickaxe wake` | Start it. Waits until players can actually connect. |
| `pickaxe sleep` | Back up the world, stop Minecraft cleanly, stop the instance. |
| `pickaxe status` | State, address, version, who's online, auto-sleep setting. |
| `pickaxe ip` | Just the address, for scripts. |

`pickaxe sleep` refuses to run if someone is still playing — pass `--force` to
override, or `--skip-backup` to skip the final snapshot.

### Running the server

| Command | What it does |
| --- | --- |
| `pickaxe console "time set day"` | Run any command on the server console. |
| `pickaxe op <player>` | Shortcut for `console "op <player>"`. |
| `pickaxe logs -n 200` | Show the server log. `--follow` polls for new lines. |
| `pickaxe ssh` | Shell on the instance via SSM — no SSH key, no open port 22. |

Console access goes through RCON on the instance, reached over AWS Systems
Manager. RCON is never exposed to the internet.

### Backups

| Command | What it does |
| --- | --- |
| `pickaxe backup` | Take a snapshot right now. |
| `pickaxe backups` | List what's in S3, with sizes and ages. |
| `pickaxe restore latest` | Replace the live world with a backup. |
| `pickaxe restore backups/world-20260809T140000Z.tar.gz` | Restore a specific one. |

Backups run hourly on their own (`backup.interval_minutes`), before every
`sleep`, and before every auto-sleep. The newest `backup.keep` are retained and
older ones are pruned. `restore` moves the world it's replacing to
`/opt/minecraft/.pickaxe-pre-restore` rather than deleting it.

### Tearing down

```bash
pickaxe destroy                   # deletes the instance and its disk, keeps backups
pickaxe destroy --delete-bucket   # also deletes the S3 bucket and every backup
```

---

## What it costs

Sleeping is the whole point. When the instance is stopped you pay for the disk
and the IP address, not the compute.

Roughly, in `us-east-1` (check current pricing for your region):

| | Awake 24/7 | Awake ~4h/day | Asleep |
| --- | --- | --- | --- |
| `t4g.medium` compute | ~$24 /mo | ~$4 /mo | $0 |
| 30 GB gp3 disk | ~$2.40 /mo | ~$2.40 /mo | ~$2.40 /mo |
| Elastic IP | ~$3.60 /mo | ~$3.60 /mo | ~$3.60 /mo |
| S3 backups | cents | cents | cents |
| **Total** | **~$30 /mo** | **~$10 /mo** | **~$6 /mo** |

AWS bills every public IPv4 address by the hour whether the instance is running
or not, which is where the ~$3.60 floor comes from. It buys you an address that
survives sleep/wake, which is worth it.

### Auto-sleep

On by default: a watchdog on the instance checks the player count every two
minutes and, once the server has been empty for `idle.shutdown_after_minutes`,
takes a backup and stops the instance. `idle.boot_grace_minutes` keeps it from
shutting down right after a wake, before anyone has had a chance to join.

```yaml
idle:
  enabled: true
  shutdown_after_minutes: 20
  boot_grace_minutes: 15
```

---

## Picking an instance size

Minecraft's tick loop is essentially single-threaded, so single-core speed and
having enough RAM matter far more than core count. Graviton (`t4g.*`) gives the
best price for that shape of work.

| Players | Instance | RAM for the JVM | Notes |
| --- | --- | --- | --- |
| 1–5 | **`t4g.medium`** (2 vCPU, 4 GB) | `ram_gb: 3` | The default. Comfortable for vanilla. |
| 1–3, budget | `t4g.small` (2 vCPU, 2 GB) | `ram_gb: 1` | Tight. Fine for a small vanilla world, not for mods. |
| 5–12 | `t4g.large` (2 vCPU, 8 GB) | `ram_gb: 6` | Headroom for a bigger world or light plugins. |
| 12–25, or heavily modded | `t4g.xlarge` (4 vCPU, 16 GB) | `ram_gb: 12` | |

**For 5 players, `t4g.medium` with `ram_gb: 3` is the right answer** — which is
what `pickaxe init` gives you if you accept the defaults. Leave ~1 GB for the OS
and never set `ram_gb` to the instance's full memory.

Two things to know about `t4g`: they're burstable, so sustained heavy load (a
large modpack, world pre-generation) can accrue surcharges — for normal play
with a handful of people this doesn't come up. And a bigger disk mostly matters
for map size and backups; 30 GB is plenty for a vanilla world.

Changing `instance_type` later is fine — `pickaxe up` resizes in place, keeping
the world. Changing `disk_size_gb` on a live server is **not**: CloudFormation
would recreate the instance and its disk, so `pickaxe up` refuses unless you
pass `--allow-replace` (back up first).

---

## config.yaml

Everything the server is, in one file. Re-run `pickaxe up` after editing.

```yaml
server:
  name: survival                  # also the CloudFormation stack name

aws:
  region: us-east-1
  instance_type: t4g.medium
  disk_size_gb: 30
  s3_bucket: pickaxe-survival-123456789012   # filled in automatically
  # profile: personal             # AWS profile, if not the default
  # key_name: my-keypair          # only if you want real SSH
  # ssh_cidr: 203.0.113.4/32      # only if you want real SSH

minecraft:
  version: latest                 # 'latest', '1.21.4', or 'keep' for a modded jar
  ram_gb: 3
  port: 25565
  motd: survival - a Pickaxe server
  local_server_path: ./world_data # or null for a fresh world

backup:
  interval_minutes: 60
  keep: 24

idle:
  enabled: true
  shutdown_after_minutes: 20
  boot_grace_minutes: 15
```

Config changes reach a **running** server immediately, and a **sleeping** one
the next time it wakes — the instance re-reads its config from S3 on every boot.

You can run more than one server from separate directories: each needs its own
`config.yaml` with a different `server.name`.

---

## How it works

```
your machine                      AWS
────────────                      ───
pickaxe up ──┬─ upload agent + config ──► S3 ──┐
             │                                 │
             └─ serverless deploy ──► CloudFormation
                                            │  │
                                            ▼  │
                          EC2 (Ubuntu 24.04) ◄─┘  pulls config + agent on boot
                            ├─ minecraft.service    (systemd, graceful stop via RCON)
                            ├─ pickaxe-backup.timer (hourly -> S3)
                            └─ pickaxe-idle.timer   (empty? -> back up, stop self)
                          Elastic IP  ── stable address across sleep/wake
```

Resources created: one EC2 instance, one Elastic IP, one security group, one IAM
role and instance profile, and one S3 bucket. The bucket holds the agent, the
rendered config, the world seed, backups, and the Serverless deployment
artifacts.

A few deliberate choices worth knowing about:

- **The instance's UserData never changes.** CloudFormation replaces an EC2
  instance when its UserData changes, which would wipe the root volume — and
  your world with it. So UserData only knows the bucket name; everything else is
  fetched from S3 at boot and can change freely.
- **The AMI is pinned after the first deploy.** Resolving "latest Ubuntu" on
  every run would replace the instance whenever Canonical published a new image.
- **Access is through SSM, not SSH.** No key pair to manage, no port 22 open to
  the internet. Set `key_name` + `ssh_cidr` if you'd rather have real SSH.
- **Serverless Framework v3**, pinned locally, because v4 requires a Serverless
  Inc. account and access key.

### Files

```
pickaxe/
├── cli.py            all commands
├── config.py         config.yaml loading and validation
├── aws.py            boto3 calls
├── ping.py           Server List Ping, for `status`
├── bootstrap.py      builds UserData, the env file, the agent bundle
├── sls.py            drives Serverless Framework
├── serverless.yml    the CloudFormation stack
└── agent/            runs on the instance
    ├── sync.sh       pull config from S3 (every boot), then install.sh
    ├── install.sh    Java, jar, server.properties, systemd units
    ├── backup.sh     world -> S3, with retention
    ├── restore.sh    S3 -> world
    ├── idle.sh       auto-sleep watchdog
    ├── mcrcon.py     RCON client
    └── props.py      server.properties editor
```

---

## Troubleshooting

**`pickaxe up` finishes but nobody can connect.** The first boot installs Java
and generates the world, which takes a few minutes past the point where
CloudFormation says it's done. `pickaxe status` will say "starting up". If it
persists, `pickaxe logs` shows the Minecraft log, and `pickaxe ssh` plus
`cat /var/log/pickaxe-install.log` shows the provisioning log.

**`could not authenticate with AWS`.** Run `aws configure`, or set
`aws.profile` in `config.yaml`.

**`node not found`.** Install Node.js 18+; Serverless Framework needs it.

**`pickaxe ssh` fails.** It needs the AWS CLI plus the
[Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html).

**The server went to sleep while I was playing.** The watchdog reads the player
count from the server console. If that fails it does nothing, so this shouldn't
happen — but you can raise `idle.shutdown_after_minutes` or set
`idle.enabled: false`.

**I want the world on my laptop.** `pickaxe backup`, then `pickaxe backups` to
get the key, then `aws s3 cp s3://<bucket>/<key> .`.

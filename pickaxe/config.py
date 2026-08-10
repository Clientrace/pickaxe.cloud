"""Loading, validating and defaulting of config.yaml."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_NAME = "config.yaml"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")


class ConfigError(Exception):
    pass


@dataclass
class ServerCfg:
    name: str = "survival"


@dataclass
class AwsCfg:
    region: str = "us-east-1"
    profile: str | None = None
    instance_type: str = "t4g.medium"
    disk_size_gb: int = 30
    s3_bucket: str | None = None
    ssh_cidr: str | None = None
    key_name: str | None = None


@dataclass
class MinecraftCfg:
    version: str = "latest"
    ram_gb: int = 3
    port: int = 25565
    motd: str = "A Pickaxe server"
    local_server_path: str | None = None


@dataclass
class BackupCfg:
    interval_minutes: int = 60
    keep: int = 24


@dataclass
class IdleCfg:
    enabled: bool = True
    shutdown_after_minutes: int = 20
    boot_grace_minutes: int = 15


@dataclass
class Config:
    server: ServerCfg = field(default_factory=ServerCfg)
    aws: AwsCfg = field(default_factory=AwsCfg)
    minecraft: MinecraftCfg = field(default_factory=MinecraftCfg)
    backup: BackupCfg = field(default_factory=BackupCfg)
    idle: IdleCfg = field(default_factory=IdleCfg)
    path: Path = field(default_factory=lambda: Path(CONFIG_NAME))

    @property
    def project_dir(self) -> Path:
        return self.path.parent.resolve()

    @property
    def stack_name(self) -> str:
        return f"pickaxe-{self.server.name}"

    @property
    def world_dir(self) -> Path | None:
        if not self.minecraft.local_server_path:
            return None
        p = Path(self.minecraft.local_server_path)
        return p if p.is_absolute() else (self.project_dir / p)


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"`{key}:` in config.yaml must be a mapping, got {type(value).__name__}")
    return value


def _build(cls, data: dict[str, Any], section: str):
    known = {f.name for f in cls.__dataclass_fields__.values()}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(
            f"unknown key(s) under `{section}:` in config.yaml: {', '.join(sorted(unknown))}"
        )
    return cls(**data)


def find_config(start: Path | None = None) -> Path | None:
    """Walk up from `start` looking for config.yaml."""
    here = (start or Path.cwd()).resolve()
    for directory in [here, *here.parents]:
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def load(path: Path | None = None) -> Config:
    path = path or find_config()
    if path is None:
        raise ConfigError(
            f"no {CONFIG_NAME} found in this directory or any parent. Run `pickaxe init` first."
        )
    if not path.is_file():
        raise ConfigError(f"{path} does not exist. Run `pickaxe init` first.")

    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")

    cfg = Config(
        server=_build(ServerCfg, _section(raw, "server"), "server"),
        aws=_build(AwsCfg, _section(raw, "aws"), "aws"),
        minecraft=_build(MinecraftCfg, _section(raw, "minecraft"), "minecraft"),
        backup=_build(BackupCfg, _section(raw, "backup"), "backup"),
        idle=_build(IdleCfg, _section(raw, "idle"), "idle"),
        path=path,
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if not NAME_RE.match(cfg.server.name):
        raise ConfigError(
            "server.name must be lowercase letters, digits and dashes, starting with a letter "
            f"(got {cfg.server.name!r}). It becomes part of the CloudFormation stack name."
        )
    if cfg.minecraft.ram_gb < 1:
        raise ConfigError("minecraft.ram_gb must be at least 1")
    if not 1 <= cfg.minecraft.port <= 65535:
        raise ConfigError("minecraft.port must be a valid TCP port")
    if cfg.aws.disk_size_gb < 8:
        raise ConfigError("aws.disk_size_gb must be at least 8")
    if cfg.backup.keep < 1:
        raise ConfigError("backup.keep must be at least 1")
    if cfg.idle.shutdown_after_minutes < 1:
        raise ConfigError("idle.shutdown_after_minutes must be at least 1")
    if cfg.aws.ssh_cidr and "/" not in cfg.aws.ssh_cidr:
        raise ConfigError(
            f"aws.ssh_cidr must be a CIDR block such as 203.0.113.4/32 (got {cfg.aws.ssh_cidr!r})"
        )
    if cfg.aws.ssh_cidr and not cfg.aws.key_name:
        raise ConfigError(
            "aws.ssh_cidr is set but aws.key_name is not -- SSH ingress is useless without a key "
            "pair. Either set aws.key_name, or drop aws.ssh_cidr and use `pickaxe ssh` (SSM)."
        )

    world = cfg.world_dir
    if world is not None and not world.is_dir():
        raise ConfigError(f"minecraft.local_server_path points at {world}, which is not a directory")


def dump(cfg: Config) -> str:
    body = {
        "server": {"name": cfg.server.name},
        "aws": {
            "region": cfg.aws.region,
            "instance_type": cfg.aws.instance_type,
            "disk_size_gb": cfg.aws.disk_size_gb,
            "s3_bucket": cfg.aws.s3_bucket,
        },
        "minecraft": {
            "version": cfg.minecraft.version,
            "ram_gb": cfg.minecraft.ram_gb,
            "port": cfg.minecraft.port,
            "motd": cfg.minecraft.motd,
            "local_server_path": cfg.minecraft.local_server_path,
        },
        "backup": {
            "interval_minutes": cfg.backup.interval_minutes,
            "keep": cfg.backup.keep,
        },
        "idle": {
            "enabled": cfg.idle.enabled,
            "shutdown_after_minutes": cfg.idle.shutdown_after_minutes,
            "boot_grace_minutes": cfg.idle.boot_grace_minutes,
        },
    }
    if cfg.aws.profile:
        body["aws"]["profile"] = cfg.aws.profile
    if cfg.aws.key_name:
        body["aws"]["key_name"] = cfg.aws.key_name
    if cfg.aws.ssh_cidr:
        body["aws"]["ssh_cidr"] = cfg.aws.ssh_cidr

    header = (
        "# Pickaxe -- Minecraft on AWS.\n"
        "# Edit this file, then run `pickaxe up` to apply the changes.\n"
        "# See README.md for what every key does.\n\n"
    )
    return header + yaml.safe_dump(body, sort_keys=False, default_flow_style=False)


def save(cfg: Config) -> None:
    cfg.path.write_text(dump(cfg))


def default_bucket_name(name: str, account_id: str) -> str:
    return f"pickaxe-{name}-{account_id}"[:63]


# --------------------------------------------------------------- settings registry

# What `pickaxe config` can read and write, and -- just as important -- what
# changing each one actually costs. `install.sh` restarts Minecraft only when a
# fingerprint of jvm.args + server.properties + .mc-version + JAVA_BIN changes,
# so timers and watchdog settings are genuinely free to change under live
# players, while anything the JVM reads at startup is not.
IMPACT_NONE = "none"          # applies with no interruption
IMPACT_RESTART = "restart"    # Minecraft restarts; players are disconnected
IMPACT_REBOOT = "reboot"      # EC2 stop/start; several minutes of downtime
IMPACT_REPLACE = "replace"    # recreates the instance and its disk
IMPACT_NEW_STACK = "new"      # deploys a *separate* server, orphaning this one

SETTINGS: dict[str, tuple[type, bool, str, str]] = {
    # path: (type, optional, impact, description)
    "server.name": (str, False, IMPACT_NEW_STACK, "Stack name; identifies the server"),
    "aws.region": (str, False, IMPACT_NEW_STACK, "AWS region"),
    "aws.profile": (str, True, IMPACT_NONE, "AWS credential profile"),
    "aws.instance_type": (str, False, IMPACT_REBOOT, "EC2 size"),
    "aws.disk_size_gb": (int, False, IMPACT_REPLACE, "Root volume size"),
    "aws.s3_bucket": (str, True, IMPACT_NEW_STACK, "Bucket for backups and config"),
    "aws.key_name": (str, True, IMPACT_NONE, "EC2 key pair, for real SSH"),
    "aws.ssh_cidr": (str, True, IMPACT_NONE, "CIDR allowed on port 22"),
    "minecraft.version": (str, False, IMPACT_RESTART, "'latest', a version, or 'keep'"),
    "minecraft.ram_gb": (int, False, IMPACT_RESTART, "JVM heap size"),
    "minecraft.port": (int, False, IMPACT_RESTART, "Game port"),
    "minecraft.motd": (str, False, IMPACT_RESTART, "Server list message"),
    "minecraft.local_server_path": (str, True, IMPACT_NONE, "World to seed from"),
    "backup.interval_minutes": (int, False, IMPACT_NONE, "How often to back up"),
    "backup.keep": (int, False, IMPACT_NONE, "Backups to retain in S3"),
    "idle.enabled": (bool, False, IMPACT_NONE, "Auto-sleep when empty"),
    "idle.shutdown_after_minutes": (int, False, IMPACT_NONE, "Minutes empty before sleeping"),
    "idle.boot_grace_minutes": (int, False, IMPACT_NONE, "Grace period after waking"),
}


def get_path(cfg: Config, path: str):
    if path not in SETTINGS:
        raise ConfigError(unknown_path_message(path))
    section, _, field = path.partition(".")
    return getattr(getattr(cfg, section), field)


def set_path(cfg: Config, path: str, raw: str) -> object:
    """Coerce `raw` to the setting's type and assign it. Returns the new value."""
    if path not in SETTINGS:
        raise ConfigError(unknown_path_message(path))
    kind, optional, _, _ = SETTINGS[path]
    value = _coerce(path, raw, kind, optional)
    section, _, field = path.partition(".")
    setattr(getattr(cfg, section), field, value)
    validate(cfg)
    return value


def _coerce(path: str, raw: str, kind: type, optional: bool):
    text = raw.strip()
    if optional and text.lower() in ("", "null", "none", "~"):
        return None
    if kind is bool:
        if text.lower() in ("true", "yes", "on", "1"):
            return True
        if text.lower() in ("false", "no", "off", "0"):
            return False
        raise ConfigError(f"{path} must be true or false (got {raw!r})")
    if kind is int:
        try:
            return int(text)
        except ValueError:
            raise ConfigError(f"{path} must be a whole number (got {raw!r})") from None
    return text


def unknown_path_message(path: str) -> str:
    from difflib import get_close_matches

    hint = get_close_matches(path, SETTINGS, n=1)
    suffix = f" Did you mean {hint[0]}?" if hint else ""
    return f"unknown setting {path!r}.{suffix} Run `pickaxe config` to list them all."

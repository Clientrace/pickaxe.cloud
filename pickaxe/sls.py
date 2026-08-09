"""Drives Serverless Framework v3.

v3 rather than v4 on purpose: v4 requires a Serverless Inc. account and access
key, which would put a signup between the user and a working server. v3 is
Apache-2.0 and needs nothing but npm.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .config import Config

PACKAGE_ROOT = Path(__file__).parent


class SlsError(Exception):
    pass


def _require(binary: str, hint: str) -> str:
    found = shutil.which(binary)
    if not found:
        raise SlsError(f"`{binary}` not found on PATH. {hint}")
    return found


def stage_dir(cfg: Config) -> Path:
    """Working directory Serverless runs in: <project>/.pickaxe/deploy."""
    target = cfg.project_dir / ".pickaxe" / "deploy"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("serverless.yml", "package.json"):
        shutil.copyfile(PACKAGE_ROOT / name, target / name)
    return target


def ensure_installed(cfg: Config, log=print) -> Path:
    _require("node", "Install Node.js 18+ (https://nodejs.org) -- Serverless Framework needs it.")
    npm = _require("npm", "Install npm (it ships with Node.js).")

    target = stage_dir(cfg)
    if not (target / "node_modules" / "serverless").is_dir():
        log("Installing Serverless Framework (one time, ~30s)...")
        subprocess.run(
            [npm, "install", "--no-audit", "--no-fund", "--loglevel", "error"],
            cwd=target,
            check=True,
        )
    return target


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "SLS_TELEMETRY_DISABLED": "1",
            "SERVERLESS_DISABLE_AUTO_UPDATE": "1",
            # Keeps v3 from trying to run its interactive onboarding wizard.
            "CI": "1",
        }
    )
    return env


def _run(cfg: Config, args: list[str], log=print) -> None:
    target = ensure_installed(cfg, log=log)
    binary = target / "node_modules" / ".bin" / "serverless"
    command = [
        str(binary),
        *args,
        "--stage",
        cfg.server.name,
        "--region",
        cfg.aws.region,
    ]
    if cfg.aws.profile:
        command += ["--aws-profile", cfg.aws.profile]

    result = subprocess.run(command, cwd=target, env=_env())
    if result.returncode != 0:
        raise SlsError(f"`serverless {args[0]}` failed with exit code {result.returncode}")


def deploy(cfg: Config, params: dict[str, str], log=print) -> None:
    args = ["deploy"]
    for key, value in params.items():
        args += ["--param", f"{key}={value}"]
    _run(cfg, args, log=log)


def remove(cfg: Config, params: dict[str, str], log=print) -> None:
    args = ["remove"]
    for key, value in params.items():
        args += ["--param", f"{key}={value}"]
    _run(cfg, args, log=log)

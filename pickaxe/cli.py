"""pickaxe -- Minecraft servers on AWS that you can put to sleep."""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import typer
from botocore.exceptions import ClientError
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import aws, bootstrap, config as cfgmod, ping, sls
from .config import Config, ConfigError

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    # Tracebacks are handled in main(); a boto stack trace is not a useful
    # answer to "my Minecraft server won't start".
    pretty_exceptions_enable=False,
    help="Minecraft servers on AWS. `pickaxe init`, `pickaxe up`, then `wake` / `sleep`.",
)
console = Console()
err_console = Console(stderr=True)

STATE_KEY = "state/deploy.json"


# --------------------------------------------------------------------------- helpers


def fail(message: str) -> None:
    err_console.print(f"[bold red]error[/] {message}")
    raise typer.Exit(1)


def load_config(path: Path | None = None) -> Config:
    try:
        return cfgmod.load(path)
    except ConfigError as exc:
        fail(str(exc))
        raise  # unreachable, keeps type checkers happy


def resolved_bucket(cfg: Config, sess) -> str:
    if cfg.aws.s3_bucket:
        return cfg.aws.s3_bucket
    bucket = cfgmod.default_bucket_name(cfg.server.name, aws.account_id(sess))
    cfg.aws.s3_bucket = bucket
    cfgmod.save(cfg)
    console.print(f"  chose S3 bucket [cyan]{bucket}[/] and saved it to config.yaml")
    return bucket


def connect(cfg: Config):
    sess = aws.session(cfg)
    try:
        aws.account_id(sess)
    except Exception as exc:  # noqa: BLE001 -- surfaces as a friendly message
        fail(
            f"could not authenticate with AWS ({exc}).\n"
            "  Run `aws configure`, or set aws.profile in config.yaml."
        )
    return sess


def stack_and_session(cfg: Config):
    sess = connect(cfg)
    try:
        stack = aws.require_stack(sess, cfg.stack_name)
    except aws.AwsError as exc:
        fail(str(exc))
        raise
    return sess, stack


def human_age(then: datetime | None) -> str:
    if then is None:
        return "unknown"
    seconds = int((datetime.now(timezone.utc) - then).total_seconds())
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# --------------------------------------------------------------------------- init


@app.command()
def init(
    directory: Path = typer.Option(Path("."), "--dir", "-d", help="Where to write config.yaml."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config.yaml."),
) -> None:
    """Create config.yaml by asking a handful of questions."""
    target = directory.resolve() / cfgmod.CONFIG_NAME
    if target.exists() and not force:
        fail(f"{target} already exists. Pass --force to overwrite it.")

    console.print(Panel.fit("[bold]Pickaxe setup[/]\nPress enter to accept the default."))

    cfg = Config(path=target)
    cfg.server.name = typer.prompt("Server name (lowercase, no spaces)", default="survival")
    cfg.aws.region = typer.prompt("AWS region", default="us-east-1")
    profile = typer.prompt("AWS profile (blank for default)", default="", show_default=False)
    cfg.aws.profile = profile or None

    cfg.aws.instance_type = typer.prompt(
        "Instance type (t4g.medium ~4GB RAM, t4g.large ~8GB)", default="t4g.medium"
    )
    cfg.minecraft.ram_gb = typer.prompt("RAM for the JVM, in GB", default=3, type=int)
    cfg.aws.disk_size_gb = typer.prompt("Disk size in GB", default=30, type=int)
    cfg.minecraft.version = typer.prompt(
        "Minecraft version ('latest', a version like 1.21.4, or 'keep')", default="latest"
    )
    cfg.minecraft.motd = typer.prompt("Server MOTD", default=f"{cfg.server.name} - a Pickaxe server")

    world = typer.prompt(
        "Path to an existing server folder to migrate (blank for a fresh world)",
        default="",
        show_default=False,
    )
    cfg.minecraft.local_server_path = world or None

    cfg.idle.enabled = typer.confirm("Auto-sleep when nobody is playing?", default=True)
    if cfg.idle.enabled:
        cfg.idle.shutdown_after_minutes = typer.prompt("  Sleep after N minutes empty", default=20, type=int)

    try:
        cfgmod.validate(cfg)
    except ConfigError as exc:
        fail(str(exc))

    cfgmod.save(cfg)
    console.print(f"\n[green]Wrote {target}[/]")
    console.print("Next: [bold]pickaxe up[/]")


# --------------------------------------------------------------------------- up


@app.command()
def up(
    reseed: bool = typer.Option(
        False,
        "--reseed",
        help="Push the local world over the live one (asks first). Otherwise seeding "
        "only happens on a brand new server.",
    ),
    allow_replace: bool = typer.Option(
        False,
        "--allow-replace",
        help="Permit changes that destroy and recreate the instance (and its disk).",
    ),
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait for the server to accept players."),
) -> None:
    """Create or update the server. Safe to run repeatedly."""
    cfg = load_config()
    sess = connect(cfg)
    bucket = resolved_bucket(cfg, sess)
    existing = aws.get_stack(sess, cfg.stack_name)

    console.print(f"[bold]{'Updating' if existing else 'Creating'}[/] server [cyan]{cfg.server.name}[/]")

    console.print("  preparing S3 bucket...")
    try:
        aws.ensure_bucket(sess, bucket)
    except aws.AwsError as exc:
        fail(str(exc))

    # -- work out AMI / disk, keeping an existing instance in place ------------
    if existing and existing.outputs.get("PickaxeInstanceId"):
        details = aws.instance_details(sess, existing.instance_id)
        # Pinning the running AMI is what keeps `up` non-destructive: resolving
        # the latest Ubuntu image every time would replace the instance -- and
        # its root volume -- whenever Canonical publishes a new build.
        ami_id = details["image_id"]
        root_device = details["root_device"]
        if details["root_size"] and details["root_size"] != cfg.aws.disk_size_gb and not allow_replace:
            fail(
                f"aws.disk_size_gb is {cfg.aws.disk_size_gb} but the live root volume is "
                f"{details['root_size']} GB. Changing it recreates the instance and erases the "
                "world.\n"
                f"  Set it back to {details['root_size']}, or run `pickaxe backup` and then "
                "`pickaxe up --allow-replace`."
            )
    else:
        aws.ensure_default_vpc(sess)
        arch = aws.instance_arch(cfg.aws.instance_type)
        console.print(f"  resolving Ubuntu 24.04 {arch} image...")
        ami_id = aws.resolve_ami(sess, arch)
        root_device = aws.root_device_name(sess, ami_id)

    # -- upload agent, config, and optionally the world -----------------------
    console.print("  uploading server agent and configuration...")
    aws.upload(sess, bucket, "bootstrap/agent.tar.gz", bootstrap.build_agent_bundle())
    aws.upload(sess, bucket, "bootstrap/pickaxe.env", bootstrap.render_env(cfg, bucket).encode())

    world_dir = cfg.world_dir
    if world_dir and (reseed or not existing):
        console.print(f"  packaging world from [cyan]{world_dir}[/]...")
        with tempfile.TemporaryDirectory() as tmp:
            archive = bootstrap.build_world_seed(world_dir, Path(tmp) / "serverdata.tar.gz")
            size_mb = archive.stat().st_size / 1_048_576
            console.print(f"  uploading world seed ({size_mb:.1f} MB)...")
            with console.status("[bold]uploading[/]"):
                aws.upload_file(sess, bucket, "seed/serverdata.tar.gz", archive)
    elif world_dir:
        console.print("  world already seeded; skipping upload (use --reseed to force)")

    user_data = bootstrap.render_user_data(cfg, bucket)
    params = {
        "s3_bucket": bucket,
        "instance_type": cfg.aws.instance_type,
        "disk_size": str(cfg.aws.disk_size_gb),
        "ami_id": ami_id,
        "root_device": root_device,
        "mc_port": str(cfg.minecraft.port),
        "key_name": cfg.aws.key_name or "none",
        "ssh_cidr": cfg.aws.ssh_cidr or "none",
        "user_data": base64.b64encode(user_data.encode()).decode(),
    }

    console.print("  deploying infrastructure (this takes a few minutes on first run)...\n")
    try:
        sls.deploy(cfg, params, log=lambda m: console.print(f"  {m}"))
    except sls.SlsError as exc:
        fail(str(exc))

    stack = aws.require_stack(sess, cfg.stack_name)
    aws.upload(
        sess,
        bucket,
        STATE_KEY,
        json.dumps({k: v for k, v in params.items() if k != "user_data"}, indent=2).encode(),
    )

    # An instance that already existed does not re-run UserData, so push the new
    # config to it directly. A brand new one is already doing this on boot.
    if existing and aws.instance_state(sess, stack.instance_id) == "running":
        console.print("\n  applying configuration to the running server...")
        try:
            aws.wait_for_ssm(sess, stack.instance_id)
            output = aws.run_shell(
                sess, stack.instance_id, ["/opt/pickaxe/sync.sh"], comment="pickaxe up"
            )
            for line in output.splitlines()[-6:]:
                console.print(f"    [dim]{line}[/]")
        except aws.AwsError as exc:
            err_console.print(f"  [yellow]warning[/] could not refresh the running server: {exc}")

        # Uploading a seed is not enough on a server that has already been
        # provisioned -- install.sh deliberately never touches an existing
        # world. Pushing it over the live world has to be asked for explicitly.
        if world_dir and reseed:
            console.print()
            console.print(
                f"[bold yellow]--reseed will replace the live world on {cfg.server.name}[/] "
                f"with your local copy from {world_dir}."
            )
            console.print(
                "The world being replaced is kept on the instance at "
                "/opt/minecraft/.pickaxe-pre-restore."
            )
            if typer.confirm("Push the local world over the live one?", default=False):
                try:
                    output = aws.run_shell(
                        sess,
                        stack.instance_id,
                        ["/opt/pickaxe/restore.sh seed/serverdata.tar.gz"],
                        comment="pickaxe up --reseed",
                    )
                    for line in output.splitlines()[-6:]:
                        console.print(f"    [dim]{line}[/]")
                except aws.AwsError as exc:
                    err_console.print(f"  [yellow]warning[/] reseed failed: {exc}")
            else:
                console.print("[dim]  skipped; the uploaded seed is unused[/]")

    ip = stack.public_ip
    console.print()
    if wait and aws.instance_state(sess, stack.instance_id) == "running":
        _wait_for_players(ip, cfg.minecraft.port)
    _print_address(cfg, ip)


def _wait_for_players(ip: str | None, port: int) -> None:
    if not ip:
        return
    with console.status("[bold]waiting for Minecraft to accept players[/] (first boot ~3-5 min)"):
        status = ping.wait_until_up(ip, port, timeout=900)
    if status is None:
        err_console.print(
            "[yellow]warning[/] the server did not answer in time. "
            "Check `pickaxe logs` -- the world may still be generating."
        )


def _print_address(cfg: Config, ip: str | None) -> None:
    address = ip if cfg.minecraft.port == 25565 else f"{ip}:{cfg.minecraft.port}"
    console.print(
        Panel.fit(
            f"[bold green]Server address[/]\n[bold]{address}[/]\n\n"
            "[dim]pickaxe sleep[/]  stop paying for compute\n"
            "[dim]pickaxe wake[/]   bring it back",
            title=cfg.server.name,
        )
    )


# --------------------------------------------------------------------------- wake / sleep


@app.command()
def wake(
    wait: bool = typer.Option(True, "--wait/--no-wait", help="Wait until players can join."),
) -> None:
    """Start the server."""
    cfg = load_config()
    sess, stack = stack_and_session(cfg)
    instance_id = stack.instance_id

    state = aws.instance_state(sess, instance_id)
    if state == "running":
        console.print("[green]Already awake.[/]")
        _print_address(cfg, stack.public_ip)
        return
    if state in ("stopping", "shutting-down"):
        console.print("Instance is still shutting down; waiting for it to settle...")
        aws.wait_for_state(sess, instance_id, "stopped", timeout=300)
    elif state != "stopped":
        fail(f"instance is in state {state!r} and cannot be started")

    console.print(f"Waking [cyan]{cfg.server.name}[/]...")
    aws.start_instance(sess, instance_id)
    with console.status("[bold]booting EC2 instance[/]"):
        aws.wait_for_state(sess, instance_id, "running", timeout=300)

    if wait:
        _wait_for_players(stack.public_ip, cfg.minecraft.port)
    _print_address(cfg, stack.public_ip)


@app.command()
def sleep(
    skip_backup: bool = typer.Option(
        False, "--skip-backup", help="Stop without taking a final backup."
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Stop even if players are online."),
) -> None:
    """Back up the world and stop the server. Storage still costs a little; compute does not."""
    cfg = load_config()
    sess, stack = stack_and_session(cfg)
    instance_id = stack.instance_id

    state = aws.instance_state(sess, instance_id)
    if state == "stopped":
        console.print("[green]Already asleep.[/]")
        return
    if state != "running":
        fail(f"instance is in state {state!r}; nothing to stop")

    if not force and stack.public_ip:
        try:
            status = ping.ping(stack.public_ip, cfg.minecraft.port, timeout=4)
            if status.players_online:
                names = ", ".join(status.sample) or f"{status.players_online} player(s)"
                fail(f"{names} still online. Pass --force to stop anyway.")
        except (OSError, ValueError):
            pass  # unreachable server -- nothing to protect

    if not skip_backup:
        console.print("Backing up the world...")
        try:
            aws.wait_for_ssm(sess, instance_id, timeout=120)
            output = aws.run_shell(
                sess, instance_id, ["/opt/pickaxe/backup.sh"], comment="pickaxe sleep"
            )
            for line in output.splitlines()[-3:]:
                console.print(f"  [dim]{line}[/]")
        except aws.AwsError as exc:
            err_console.print(f"[yellow]warning[/] backup failed: {exc}")
            if not typer.confirm("Stop the server anyway?", default=False):
                raise typer.Exit(1)

    console.print("Stopping the instance (this also stops Minecraft cleanly)...")
    aws.stop_instance(sess, instance_id)
    with console.status("[bold]shutting down[/]"):
        aws.wait_for_state(sess, instance_id, "stopped", timeout=300)
    console.print(f"[green]{cfg.server.name} is asleep.[/] Wake it with [bold]pickaxe wake[/].")


# --------------------------------------------------------------------------- status


@app.command()
def status() -> None:
    """Show whether the server is up, its address, and who is playing."""
    cfg = load_config()
    sess = connect(cfg)
    stack = aws.get_stack(sess, cfg.stack_name)

    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column(style="dim")
    table.add_column()
    table.add_row("server", cfg.server.name)
    table.add_row("region", cfg.aws.region)

    if stack is None:
        table.add_row("state", "[yellow]not deployed[/]")
        console.print(table)
        console.print("\nRun [bold]pickaxe up[/] to create it.")
        return

    details = aws.instance_details(sess, stack.instance_id)
    state = details["state"]
    colour = {"running": "green", "stopped": "yellow"}.get(state, "cyan")
    table.add_row("state", f"[{colour}]{state}[/]")
    table.add_row("instance", f"{details['instance_type']} ({stack.instance_id})")
    table.add_row("disk", f"{details['root_size']} GB")
    address = stack.public_ip or "-"
    if cfg.minecraft.port != 25565:
        address = f"{address}:{cfg.minecraft.port}"
    table.add_row("address", f"[bold]{address}[/]")
    if state == "running":
        table.add_row("up for", human_age(details["launch_time"]))

    if state == "running" and stack.public_ip:
        try:
            info = ping.ping(stack.public_ip, cfg.minecraft.port, timeout=4)
            table.add_row("minecraft", f"{info.version} ({info.latency_ms} ms)")
            players = f"{info.players_online}/{info.players_max}"
            if info.sample:
                players += "  " + ", ".join(info.sample)
            table.add_row("players", players)
            table.add_row("motd", info.motd)
        except (OSError, ValueError):
            table.add_row("minecraft", "[yellow]starting up (not accepting connections yet)[/]")

    if cfg.idle.enabled:
        table.add_row("auto-sleep", f"after {cfg.idle.shutdown_after_minutes} min empty")
    else:
        table.add_row("auto-sleep", "[yellow]disabled[/]")

    console.print(table)


@app.command()
def ip() -> None:
    """Print just the server address (handy for scripts)."""
    cfg = load_config()
    _, stack = stack_and_session(cfg)
    if not stack.public_ip:
        fail("the stack has no public IP yet")
    address = stack.public_ip
    print(address if cfg.minecraft.port == 25565 else f"{address}:{cfg.minecraft.port}")


# --------------------------------------------------------------------------- console access


@app.command(name="console")
def console_cmd(
    command: list[str] = typer.Argument(..., help='Minecraft command, e.g. "time set day".'),
) -> None:
    """Run a command on the server console (RCON, via SSM)."""
    cfg = load_config()
    sess, stack = stack_and_session(cfg)
    _require_running(sess, stack.instance_id)

    joined = " ".join(command)
    try:
        output = aws.run_shell(
            sess,
            stack.instance_id,
            [f"/usr/local/bin/pickaxe-rcon {_shell_quote(joined)}"],
            timeout=60,
            comment="pickaxe console",
        )
    except aws.AwsError as exc:
        fail(str(exc))
        return
    console.print(output or "[dim](no output)[/]")


@app.command()
def op(player: str) -> None:
    """Grant operator status to a player."""
    console_cmd([f"op {player}"])


def _shell_quote(value: str) -> str:
    return shlex.quote(value)


def _require_running(sess, instance_id: str) -> None:
    state = aws.instance_state(sess, instance_id)
    if state != "running":
        fail(f"the server is {state}. Run `pickaxe wake` first.")


@app.command()
def logs(
    lines: int = typer.Option(80, "--lines", "-n", help="How many log lines to show."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep polling for new lines."),
) -> None:
    """Show the Minecraft server log."""
    cfg = load_config()
    sess, stack = stack_and_session(cfg)
    _require_running(sess, stack.instance_id)

    def fetch(extra: str) -> str:
        return aws.run_shell(
            sess,
            stack.instance_id,
            [f"journalctl -u minecraft.service --no-pager -o short-iso {extra}"],
            timeout=60,
            comment="pickaxe logs",
        )

    try:
        output = fetch(f"-n {lines}")
    except aws.AwsError as exc:
        fail(str(exc))
        return
    console.print(output, highlight=False)

    if not follow:
        return
    console.print("[dim](polling every 10s, ctrl-c to stop)[/]")
    seen = output.splitlines()[-1] if output.strip() else ""
    try:
        while True:
            time.sleep(10)
            fresh = fetch("-n 200")
            lines_out = fresh.splitlines()
            if seen in lines_out:
                lines_out = lines_out[lines_out.index(seen) + 1 :]
            if lines_out:
                console.print("\n".join(lines_out), highlight=False)
                seen = lines_out[-1]
    except KeyboardInterrupt:
        pass


@app.command()
def ssh() -> None:
    """Open a shell on the instance via SSM Session Manager (no SSH key needed)."""
    cfg = load_config()
    sess, stack = stack_and_session(cfg)
    _require_running(sess, stack.instance_id)

    binary = shutil.which("aws")
    if not binary:
        fail(
            "the AWS CLI is required for `pickaxe ssh`.\n"
            "  Install it, plus the Session Manager plugin:\n"
            "  https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html"
        )
    argv = [binary, "ssm", "start-session", "--target", stack.instance_id, "--region", cfg.aws.region]
    if cfg.aws.profile:
        argv += ["--profile", cfg.aws.profile]
    console.print(f"[dim]{' '.join(argv)}[/]")
    os.execv(binary, argv)


# --------------------------------------------------------------------------- backups


@app.command()
def backup() -> None:
    """Take a world backup right now."""
    cfg = load_config()
    sess, stack = stack_and_session(cfg)
    _require_running(sess, stack.instance_id)

    console.print("Backing up...")
    try:
        aws.wait_for_ssm(sess, stack.instance_id, timeout=120)
        output = aws.run_shell(
            sess, stack.instance_id, ["/opt/pickaxe/backup.sh"], comment="pickaxe backup"
        )
    except aws.AwsError as exc:
        fail(str(exc))
        return
    console.print(output or "[green]done[/]")


@app.command()
def backups() -> None:
    """List backups stored in S3."""
    cfg = load_config()
    sess = connect(cfg)
    bucket = cfg.aws.s3_bucket or resolved_bucket(cfg, sess)

    items = aws.list_backups(sess, bucket)
    if not items:
        console.print("[yellow]No backups yet.[/]")
        return

    table = Table(title=f"s3://{bucket}/backups/")
    table.add_column("key")
    table.add_column("size", justify="right")
    table.add_column("age", justify="right")
    for obj in items:
        table.add_row(obj["Key"], f"{obj['Size'] / 1_048_576:.1f} MB", human_age(obj["LastModified"]))
    console.print(table)


@app.command()
def restore(
    key: str = typer.Argument("latest", help="Backup key from `pickaxe backups`, or 'latest'."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Replace the live world with a backup."""
    cfg = load_config()
    sess, stack = stack_and_session(cfg)
    _require_running(sess, stack.instance_id)

    if not yes:
        console.print(f"[bold yellow]This replaces the live world on {cfg.server.name}.[/]")
        console.print("The current world is kept on disk at /opt/minecraft/.pickaxe-pre-restore.")
        if not typer.confirm(f"Restore {key}?", default=False):
            raise typer.Exit(1)

    try:
        output = aws.run_shell(
            sess,
            stack.instance_id,
            [f"/opt/pickaxe/restore.sh {_shell_quote(key)}"],
            comment="pickaxe restore",
        )
    except aws.AwsError as exc:
        fail(str(exc))
        return
    console.print(output)


# --------------------------------------------------------------------------- destroy


@app.command()
def destroy(
    delete_bucket: bool = typer.Option(
        False, "--delete-bucket", help="Also delete the S3 bucket and every backup in it."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Tear down the server. Backups in S3 are kept unless --delete-bucket."""
    cfg = load_config()
    sess = connect(cfg)
    stack = aws.get_stack(sess, cfg.stack_name)
    bucket = cfg.aws.s3_bucket

    if stack is None and not delete_bucket:
        console.print("[yellow]Nothing to destroy.[/]")
        return

    console.print(f"[bold red]This deletes the EC2 instance and its disk for {cfg.server.name}.[/]")
    if delete_bucket:
        console.print(f"[bold red]It also deletes s3://{bucket} and every backup in it.[/]")
    else:
        console.print(f"Backups in s3://{bucket} will be kept.")

    if not yes and typer.prompt("Type the server name to confirm") != cfg.server.name:
        console.print("Aborted.")
        raise typer.Exit(1)

    if stack is not None:
        params = {
            "s3_bucket": bucket or "",
            "instance_type": cfg.aws.instance_type,
            "disk_size": str(cfg.aws.disk_size_gb),
            "ami_id": "ami-unused-on-remove",
            "root_device": "/dev/sda1",
            "mc_port": str(cfg.minecraft.port),
            "key_name": cfg.aws.key_name or "",
            "ssh_cidr": cfg.aws.ssh_cidr or "",
            "user_data": "none",
        }
        try:
            sls.remove(cfg, params, log=lambda m: console.print(f"  {m}"))
        except sls.SlsError as exc:
            fail(str(exc))

    if delete_bucket and bucket:
        console.print(f"  emptying s3://{bucket}...")
        aws.empty_bucket(sess, bucket)
        sess.client("s3").delete_bucket(Bucket=bucket)

    console.print("[green]Done.[/]")


def main() -> None:
    try:
        app()
    except KeyboardInterrupt:
        err_console.print("\ninterrupted")
        sys.exit(130)
    except (aws.AwsError, sls.SlsError, ConfigError) as exc:
        err_console.print(f"[bold red]error[/] {exc}")
        sys.exit(1)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        err_console.print(f"[bold red]AWS error[/] {error.get('Code')}: {error.get('Message')}")
        if error.get("Code") in ("AccessDenied", "UnauthorizedOperation"):
            err_console.print(
                "[dim]Your credentials need permission to manage EC2, IAM, S3, SSM and "
                "CloudFormation.[/]"
            )
        sys.exit(1)


if __name__ == "__main__":
    main()

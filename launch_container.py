#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import os
import socket
import stat
import subprocess
from pathlib import Path


CONTAINER_NAME = "hermes-master-v0182"
IMAGE = "nousresearch/hermes-agent@sha256:3db34ce19adfa080736a2a3feb0316dbcccc588faa9afe7fd8ae1c03b4f1a53a"
ENV_PATH = Path("/opt/data/.env")
HOME_PATH = Path("/opt/data/state/hermes-master/home")
PLUGIN_PATH = Path("/opt/data/services/hermes-codex-broker/hermes_plugin")
IPC_PATH = Path("/opt/data/state/hermes-codex-broker/ipc")
SOCKET_PATH = IPC_PATH / "broker.sock"
RUNTIME_UID = 1000
RUNTIME_GID = 1000

MASTER_CHANNEL = "1526744995623862272"
WORK_CHANNEL = "1526751882092220426"
REQUIRED_SOURCE_KEYS = ("HERMES_DISCORD_BOT_TOKEN", "DEEPSEEK_API_KEY", "DISCORD_ALLOWED_USERS")
HEALTH_COMMAND = (
    "python3 -B -c 'import json,pathlib;"
    "d=json.loads(pathlib.Path(\"/opt/data/gateway_state.json\").read_text());"
    "p=int(d.get(\"pid\",0));"
    "assert d.get(\"gateway_state\")==\"running\";"
    "assert d.get(\"platforms\",{}).get(\"discord\",{}).get(\"state\")==\"connected\";"
    "assert p>1 and pathlib.Path(f\"/proc/{p}\").is_dir()'"
)


class LaunchError(RuntimeError):
    pass


def _dotenv_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[:1] in {"'", '"'}:
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError) as exc:
            raise LaunchError("invalid quoted value in .env") from exc
        if not isinstance(parsed, str):
            raise LaunchError("non-string value in .env")
        return parsed
    marker = value.find(" #")
    return (value[:marker] if marker >= 0 else value).strip()


def load_selected_env(path: Path = ENV_PATH) -> dict[str, str]:
    try:
        path_info = path.lstat()
    except OSError as exc:
        raise LaunchError("/opt/data/.env is missing") from exc
    if not stat.S_ISREG(path_info.st_mode) or path.is_symlink():
        raise LaunchError("/opt/data/.env is missing")
    if stat.S_IMODE(path_info.st_mode) & 0o077:
        raise LaunchError("/opt/data/.env permissions must not grant group or other access")
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise LaunchError("could not read /opt/data/.env") from exc
    selected: dict[str, str] = {}
    for raw_line in raw_lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if separator and key in REQUIRED_SOURCE_KEYS:
            selected[key] = _dotenv_value(raw_value)
    missing = [key for key in REQUIRED_SOURCE_KEYS if not selected.get(key)]
    if missing:
        raise LaunchError("required environment names missing: " + ", ".join(missing))
    users = [entry.strip() for entry in selected["DISCORD_ALLOWED_USERS"].split(",") if entry.strip()]
    if not users or any(not (entry.isdigit() and 15 <= len(entry) <= 22) for entry in users):
        raise LaunchError("DISCORD_ALLOWED_USERS must contain only numeric Discord user IDs")
    return selected


def docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def container_exists() -> bool:
    try:
        result = subprocess.run(
            ["docker", "container", "inspect", CONTAINER_NAME],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def image_exists() -> bool:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", IMAGE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _private_runtime_directory(path: Path, *, group_access: bool = False) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    mode = stat.S_IMODE(info.st_mode)
    forbidden = 0o007 if group_access else 0o077
    return (
        stat.S_ISDIR(info.st_mode)
        and not path.is_symlink()
        and info.st_uid == RUNTIME_UID
        and info.st_gid == RUNTIME_GID
        and mode & 0o700 == 0o700
        and not mode & forbidden
    )


def _private_runtime_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    mode = stat.S_IMODE(info.st_mode)
    return (
        stat.S_ISREG(info.st_mode)
        and not path.is_symlink()
        and info.st_uid == RUNTIME_UID
        and info.st_gid == RUNTIME_GID
        and mode & 0o600 == 0o600
        # Hermes normalizes runtime config files to 0640.  Group-read is safe
        # here because the dedicated runtime home itself is 0700; group-write,
        # group-execute, and every permission for others remain forbidden.
        and not mode & 0o037
    )


def _broker_socket_ready(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISSOCK(info.st_mode)
        and info.st_uid == RUNTIME_UID
        and info.st_gid == RUNTIME_GID
        and not stat.S_IMODE(info.st_mode) & 0o007
    )


def local_checks(require_socket: bool) -> list[tuple[str, bool]]:
    selected = load_selected_env()
    del selected
    checks = [
        ("docker", docker_available()),
        ("pinned_image", image_exists()),
        ("dedicated_home", _private_runtime_directory(HOME_PATH)),
        ("config", _private_runtime_file(HOME_PATH / "config.yaml")),
        ("soul", _private_runtime_file(HOME_PATH / "SOUL.md")),
        ("plugin", (PLUGIN_PATH / "plugin.yaml").is_file() and (PLUGIN_PATH / "__init__.py").is_file()),
        ("ipc", _private_runtime_directory(IPC_PATH, group_access=True)),
    ]
    if require_socket:
        checks.append(("broker_socket", _broker_socket_ready(SOCKET_PATH)))
    return checks


def build_docker_argv() -> tuple[list[str], dict[str, str]]:
    selected = load_selected_env()
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"),
        "HOME": os.environ.get("HOME", "/home/hermes"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "DISCORD_BOT_TOKEN": selected["HERMES_DISCORD_BOT_TOKEN"],
        "DEEPSEEK_API_KEY": selected["DEEPSEEK_API_KEY"],
        "DISCORD_ALLOWED_USERS": selected["DISCORD_ALLOWED_USERS"],
    }
    # The official 0.18.2 image boots through s6 as root, adjusts the runtime
    # UID/GID, then drops to that account.  It therefore cannot use Docker's
    # --user or --read-only flags during initialization.  Capabilities are
    # reduced to the set required by that bootstrap instead.
    argv = [
        "docker",
        "run",
        "--detach",
        "--pull",
        "never",
        "--name",
        CONTAINER_NAME,
        "--hostname",
        "hermes-master",
        "--restart",
        "no",
        "--stop-timeout",
        "30",
        "--pids-limit",
        "256",
        "--memory",
        "2g",
        "--cpus",
        "2.0",
        "--security-opt",
        "no-new-privileges:true",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "DAC_OVERRIDE",
        "--cap-add",
        "FOWNER",
        "--cap-add",
        "KILL",
        "--cap-add",
        "SETGID",
        "--cap-add",
        "SETUID",
        "--log-opt",
        "max-size=10m",
        "--log-opt",
        "max-file=3",
        "--health-cmd",
        HEALTH_COMMAND,
        "--health-start-period",
        "120s",
        "--health-interval",
        "30s",
        "--health-timeout",
        "10s",
        "--health-retries",
        "3",
        "--label",
        "local.service=hermes-master",
        "--label",
        "local.hermes.version=0.18.2",
        # These are deliberately the only three mounts.  In particular, the
        # stock and TFT projects and the Docker socket never enter Hermes.
        "--mount",
        f"type=bind,src={HOME_PATH},dst=/opt/data",
        "--mount",
        f"type=bind,src={PLUGIN_PATH},dst=/opt/data/plugins/hermes-codex-bridge,readonly",
        "--mount",
        f"type=bind,src={IPC_PATH},dst=/run/hermes-codex,readonly",
        "--env",
        "DISCORD_BOT_TOKEN",
        "--env",
        "DEEPSEEK_API_KEY",
        "--env",
        "DISCORD_ALLOWED_USERS",
        "--env",
        f"HERMES_UID={RUNTIME_UID}",
        "--env",
        f"HERMES_GID={RUNTIME_GID}",
        "--env",
        "HERMES_HOME=/opt/data",
        "--env",
        "HERMES_DISABLE_LAZY_INSTALLS=1",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
        "--env",
        "TZ=Asia/Seoul",
        "--env",
        "DISCORD_COMMAND_SYNC_POLICY=off",
        "--env",
        "DISCORD_ALLOW_BOTS=none",
        "--env",
        f"DISCORD_ALLOWED_CHANNELS={MASTER_CHANNEL}",
        "--env",
        f"DISCORD_FREE_RESPONSE_CHANNELS={MASTER_CHANNEL}",
        "--env",
        f"DISCORD_IGNORED_CHANNELS={WORK_CHANNEL}",
        "--env",
        "DISCORD_REQUIRE_MENTION=false",
        "--env",
        f"HERMES_MASTER_CHANNEL_ID={MASTER_CHANNEL}",
        "--env",
        f"HERMES_WORK_CHANNEL_ID={WORK_CHANNEL}",
        "--env",
        "HERMES_CODEX_BROKER_SOCKET=/run/hermes-codex/broker.sock",
        "--env",
        "HERMES_CODEX_WEBHOOK_NAME=Captain Hook",
        IMAGE,
        "gateway",
        "run",
    ]
    return argv, env


def create_container() -> int:
    checks = local_checks(require_socket=True)
    failed = [name for name, ok in checks if not ok]
    if failed:
        raise LaunchError("preflight checks failed: " + ", ".join(failed))
    if container_exists():
        raise LaunchError(f"container {CONTAINER_NAME} already exists; refusing to replace it")
    argv, env = build_docker_argv()
    try:
        result = subprocess.run(
            argv,
            env=env,
            check=False,
            text=True,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LaunchError("docker create/start failed without exposing its raw environment") from exc
    if result.returncode != 0:
        raise LaunchError("docker create/start failed without exposing its raw environment")
    print(f"created_and_started={CONTAINER_NAME}")
    print("restart_policy=no")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Secret-safe launcher for the isolated Hermes 0.18.2 container")
    parser.add_argument("--check", action="store_true", help="run non-secret preflight checks")
    parser.add_argument("--create", action="store_true", help="create and start the pinned container with restart=no")
    args = parser.parse_args()
    if args.check == args.create:
        parser.error("choose exactly one of --check or --create")
    try:
        if args.check:
            checks = local_checks(require_socket=False)
            for name, ok in checks:
                print(f"{name}: {'ok' if ok else 'failed'}")
            return 0 if all(ok for _, ok in checks) else 1
        return create_container()
    except LaunchError as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

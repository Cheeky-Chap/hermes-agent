from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SERVICE_ROOT = Path("/opt/data/services/hermes-codex-broker")
STATE_ROOT = Path("/opt/data/state/hermes-codex-broker")
IPC_ROOT = STATE_ROOT / "ipc"
SOCKET_PATH = IPC_ROOT / "broker.sock"
JOBS_PATH = STATE_ROOT / "jobs.json"
BACKUP_ROOT = STATE_ROOT / "job-backups"
STAGING_ROOT = STATE_ROOT / "staging"
LOG_ROOT = Path("/opt/data/logs/hermes-codex-broker")

WORKSPACES: dict[str, Path] = {
    "stocks": Path("/opt/data/projects/stocks"),
    "tft": Path("/opt/data/projects/tft"),
    "misaka": Path("/opt/data/services/misaka-discord-gateway"),
    "daily-report": Path("/opt/data/services/daily-report"),
}

JOB_ID_RE = re.compile(r"^HC-[0-9]{8}-[A-F0-9]{4}$")
MAX_OBJECTIVE_CHARS = 8_000
MAX_SOCKET_LINE = 512 * 1024
SCOPE_SCHEMA_VERSION = 2
VALIDATION_PROFILE = "offline-no-trading-v1"
BROKER_VERSION = "1.0.0"
CODEX_CLI_VERSION = "0.144.3"
MAX_SCOPE_FILES = 30
MAX_CHANGED_FILE_BYTES = 2 * 1024 * 1024


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def compute_scope_hash(job: dict[str, Any]) -> str:
    payload = {
        "schema_version": SCOPE_SCHEMA_VERSION,
        "job_id": job.get("id"),
        "workspace": job.get("workspace"),
        "objective_sha256": sha256_text(str(job.get("objective") or "")),
        "analysis_sha256": sha256_text(str(job.get("analysis_result") or "")),
        "workspace_fingerprint": job.get("workspace_fingerprint"),
        "scope_entries": job.get("scope_entries") or [],
        "validation_profile": VALIDATION_PROFILE,
        "broker_version": BROKER_VERSION,
        "codex_cli_version": CODEX_CLI_VERSION,
        "limits": {
            "max_scope_files": MAX_SCOPE_FILES,
            "max_changed_file_bytes": MAX_CHANGED_FILE_BYTES,
        },
    }
    return sha256_text(canonical_json(payload)).upper()


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: Path, value: Any, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        fsync_directory(path.parent)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def resolve_workspace(key: str) -> Path:
    if key not in WORKSPACES:
        raise ValueError(f"unsupported workspace: {key}")
    configured = WORKSPACES[key]
    if configured.is_symlink():
        raise ValueError("workspace root must not be a symlink")
    resolved = configured.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("workspace is not a directory")
    if resolved == Path("/opt/data"):
        raise ValueError("the /opt/data root is never an allowed workspace")
    return resolved


def public_job(job: dict[str, Any], include_results: bool = True) -> dict[str, Any]:
    fields = (
        "id",
        "status",
        "workspace",
        "objective",
        "created_at",
        "updated_at",
        "round",
        "scope_hash",
        "scope_paths",
        "scope_entries",
        "scope_summary",
        "error_code",
        "error_message",
        "changed_paths",
        "version",
    )
    out = {name: job.get(name) for name in fields if name in job}
    if include_results:
        for name in ("analysis_result", "apply_result"):
            value = job.get(name)
            if isinstance(value, str):
                out[name] = value[:60_000]
    return out

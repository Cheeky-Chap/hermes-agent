from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


CODEX_BIN = Path(
    "/home/hermes/.codex/packages/standalone/releases/"
    "0.144.3-x86_64-unknown-linux-musl/bin/codex"
)
BWRAP_BIN = Path("/usr/bin/bwrap")
CODEX_HOME = Path("/home/hermes/.codex")
AUTH_WRAPPER = Path(__file__).with_name("auth_wrapper.py")
AUTH_PATH = CODEX_HOME / "auth.json"
SANDBOX_CODEX_BIN = "/opt/codex/codex"
SANDBOX_AUTH_WRAPPER = "/opt/codex/auth_wrapper.py"
SANDBOX_CODEX_HOME = "/home/codex/.codex"
SANDBOX_AUTH_SEED = "/run/codex-auth-seed"
MODEL = "gpt-5.6-sol"
RUN_TIMEOUT_SECONDS = 300
TERMINATE_GRACE_SECONDS = 5
MAX_PROMPT_BYTES = 96 * 1024
MAX_STDOUT_BYTES = 2 * 1024 * 1024
MAX_STDERR_BYTES = 2 * 1024 * 1024
STDERR_TAIL_BYTES = 16 * 1024
MAX_RESULT_CHARS = 60_000
MAX_HASH_FILE_BYTES = 20 * 1024 * 1024
MAX_AUTH_BYTES = 128 * 1024
MAX_SECRET_SOURCE_BYTES = 1024 * 1024

KNOWN_ENV_PATHS = (
    Path("/opt/data/.env"),
    Path("/opt/data/configs/.env"),
    Path("/opt/data/configs/daily-report.env"),
    Path("/opt/data/shared/.env"),
)
KNOWN_AUTH_PATHS = (AUTH_PATH,)

EXCLUDED_DIR_NAMES = {
    ".git",
    ".codex",
    ".hg",
    ".svn",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
    "trading_venv",
    "cache",
    "caches",
    "logs",
    "state",
    "backups",
    "models",
    "datasets",
}

SECRET_BASENAMES = {
    "auth.json",
    "credentials.json",
    "secrets.json",
    "service-account.json",
    "id_rsa",
    "id_ed25519",
}
SECRET_SUFFIXES = {".pem", ".p12", ".pfx", ".key"}
SECRET_DIR_NAMES = {".ssh", "secrets", "credentials"}

SECRET_OUTPUT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "discord_webhook",
        re.compile(r"https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9._-]+", re.I),
    ),
    ("openai_style_key", re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{20,}\b")),
    ("github_token", re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b", re.I)),
    ("discord_token", re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}\b")),
    (
        "secret_assignment",
        re.compile(
            r"(?im)^\s*(?:export\s+)?[A-Z0-9_]*(?:API_KEY|TOKEN|WEBHOOK|SECRET|PASSWORD)\s*=\s*[^\s<]{8,}\s*$"
        ),
    ),
)

SCOPE_RE = re.compile(r"<codex_scope>\s*(\{.*?\})\s*</codex_scope>", re.S | re.I)


class RunnerError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_SENSITIVE_NAME_RE = re.compile(
    r"(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|WEBHOOK|CREDENTIAL|PRIVATE[_-]?KEY)", re.I
)
_PLACEHOLDER_VALUES = {
    b"changeme",
    b"example",
    b"placeholder",
    b"replace-me",
    b"replace_me",
    b"your-token-here",
}


def _safe_secret_label(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]", "_", value)[:120]
    return cleaned or "known_secret"


def _read_private_regular(path: Path, limit: int) -> bytes | None:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RunnerError("secret_source_unreadable", f"secret source could not be read: {path}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise RunnerError("secret_source_invalid", f"secret source has an invalid type or size: {path}")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > limit:
            raise RunnerError("secret_source_invalid", f"secret source is too large: {path}")
        return payload
    finally:
        os.close(fd)


def _clean_secret_value(raw: bytes) -> bytes | None:
    value = raw.strip()
    if len(value) >= 2 and value[:1] == value[-1:] and value[:1] in {b"'", b'"'}:
        value = value[1:-1].strip()
    if len(value) < 8 or value.lower() in _PLACEHOLDER_VALUES:
        return None
    return value


class SecretScanner:
    """Detect known credentials and high-confidence secret formats.

    Secret values remain private byte strings.  Scanner results contain only a
    sanitized source label and never the matching value.
    """

    def __init__(self, values: Iterable[tuple[str, bytes | str]] = ()) -> None:
        needles: list[tuple[str, tuple[bytes, ...]]] = []
        seen: set[bytes] = set()
        for raw_label, raw_value in values:
            encoded = raw_value.encode("utf-8") if isinstance(raw_value, str) else bytes(raw_value)
            value = _clean_secret_value(encoded)
            if value is None or value in seen:
                continue
            seen.add(value)
            variants = self._variants(value)
            needles.append((_safe_secret_label(raw_label), variants))
        self._needles = tuple(needles)

    @staticmethod
    def _variants(value: bytes) -> tuple[bytes, ...]:
        variants: set[bytes] = {value}
        try:
            text = value.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        if text:
            variants.add(json.dumps(text, ensure_ascii=False)[1:-1].encode("utf-8"))
            variants.add(urllib.parse.quote(text, safe="").encode("ascii"))
            variants.add(urllib.parse.quote_plus(text, safe="").encode("ascii"))
        if len(value) >= 12:
            variants.add(base64.b64encode(value))
            variants.add(base64.urlsafe_b64encode(value))
            variants.add(base64.urlsafe_b64encode(value).rstrip(b"="))
        return tuple(sorted((item for item in variants if len(item) >= 8), key=len, reverse=True))

    @classmethod
    def from_known_sources(
        cls,
        env_paths: Iterable[Path] | None = None,
        auth_paths: Iterable[Path] | None = None,
        *,
        include_process_env: bool = True,
    ) -> "SecretScanner":
        values: list[tuple[str, bytes | str]] = []
        for path in tuple(env_paths) if env_paths is not None else KNOWN_ENV_PATHS:
            payload = _read_private_regular(Path(path), MAX_SECRET_SOURCE_BYTES)
            if payload is None:
                continue
            for line in payload.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith(b"#"):
                    continue
                if stripped.startswith(b"export "):
                    stripped = stripped[7:].lstrip()
                key_raw, separator, value_raw = stripped.partition(b"=")
                if not separator:
                    continue
                try:
                    key = key_raw.strip().decode("ascii")
                except UnicodeDecodeError:
                    continue
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or not _SENSITIVE_NAME_RE.search(key):
                    continue
                value = _clean_secret_value(value_raw)
                if value is not None:
                    values.append((f"env:{key}", value))

        for path in tuple(auth_paths) if auth_paths is not None else KNOWN_AUTH_PATHS:
            payload = _read_private_regular(Path(path), MAX_AUTH_BYTES)
            if payload is None:
                continue
            try:
                parsed = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RunnerError("secret_source_invalid", f"authentication source is invalid: {path}") from exc

            def collect(value: Any, prefix: str = "auth") -> None:
                if isinstance(value, dict):
                    for key, child in value.items():
                        name = str(key)
                        child_prefix = f"{prefix}.{_safe_secret_label(name)}"
                        if isinstance(child, str) and _SENSITIVE_NAME_RE.search(name):
                            cleaned = _clean_secret_value(child.encode("utf-8"))
                            if cleaned is not None:
                                values.append((child_prefix, cleaned))
                        else:
                            collect(child, child_prefix)
                elif isinstance(value, list):
                    for index, child in enumerate(value):
                        collect(child, f"{prefix}.{index}")

            collect(parsed)

        if include_process_env:
            for key, value in os.environ.items():
                if _SENSITIVE_NAME_RE.search(key):
                    cleaned = _clean_secret_value(value.encode("utf-8"))
                    if cleaned is not None:
                        values.append((f"process:{key}", cleaned))
        return cls(values)

    def scan_bytes(self, data: bytes | bytearray | memoryview) -> str | None:
        payload = bytes(data)
        for label, variants in self._needles:
            if any(needle in payload for needle in variants):
                return label
        text = payload.decode("utf-8", errors="replace")
        for name, pattern in SECRET_OUTPUT_PATTERNS:
            if pattern.search(text):
                return name
        return None

    def scan_text(self, text: str) -> str | None:
        return self.scan_bytes(text.encode("utf-8", errors="replace"))


@dataclass
class CodexResult:
    text: str
    thread_id: str | None
    exit_code: int
    stderr_tail: str


@dataclass(frozen=True)
class FileRecord:
    kind: str
    digest: str
    size: int
    mode: int


def is_secret_relative(path: Path) -> bool:
    parts = {part.lower() for part in path.parts}
    base = path.name.lower()
    if parts & SECRET_DIR_NAMES:
        return True
    if base.startswith(".env"):
        return True
    if base in SECRET_BASENAMES:
        return True
    if path.suffix.lower() in SECRET_SUFFIXES:
        return True
    if base.endswith(".env") or ".secret." in base:
        return True
    return False


def _walk_files(root: Path) -> Iterable[tuple[Path, Path]]:
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs: list[str] = []
        for name in dirnames:
            full = current_path / name
            rel = full.relative_to(root)
            if full.is_symlink() or name in EXCLUDED_DIR_NAMES or is_secret_relative(rel):
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in filenames:
            full = current_path / name
            rel = full.relative_to(root)
            if full.is_symlink() or is_secret_relative(rel):
                continue
            yield full, rel


def _file_record(path: Path) -> FileRecord:
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if stat.S_ISLNK(info.st_mode):
        target = os.readlink(path)
        return FileRecord("symlink", hashlib.sha256(target.encode()).hexdigest(), len(target), mode)
    if not stat.S_ISREG(info.st_mode):
        return FileRecord("other", "", info.st_size, mode)
    if info.st_size > MAX_HASH_FILE_BYTES:
        raise RunnerError("workspace_file_too_large", f"workspace file exceeds hashing limit: {path.name}")
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RunnerError("workspace_file_unreadable", f"workspace file could not be opened: {path.name}") from exc
    digest = hashlib.sha256()
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != info.st_dev
            or opened.st_ino != info.st_ino
        ):
            raise RunnerError("workspace_changed", f"workspace file changed while opening: {path.name}")
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        finished = os.fstat(fd)
        if (
            finished.st_size != opened.st_size
            or finished.st_mtime_ns != opened.st_mtime_ns
            or finished.st_ctime_ns != opened.st_ctime_ns
        ):
            raise RunnerError("workspace_changed", f"workspace file changed while hashing: {path.name}")
        return FileRecord("file", digest.hexdigest(), opened.st_size, stat.S_IMODE(opened.st_mode))
    finally:
        os.close(fd)


def snapshot_tree(root: Path) -> dict[str, FileRecord]:
    records: dict[str, FileRecord] = {}
    for full, rel in _walk_files(root):
        records[rel.as_posix()] = _file_record(full)
    return records


def workspace_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for rel, record in sorted(snapshot_tree(root).items()):
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(record.kind.encode())
        digest.update(b"\0")
        digest.update(record.digest.encode())
        digest.update(b"\0")
        digest.update(str(record.mode).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def validate_workspace_symlinks(root: Path) -> None:
    resolved_root = root.resolve()
    for full, rel in _walk_files(root):
        if not full.is_symlink():
            continue
        target = (full.parent / os.readlink(full)).resolve(strict=False)
        try:
            target.relative_to(resolved_root)
        except ValueError as exc:
            raise RunnerError("unsafe_symlink", f"workspace contains an external symlink: {rel}") from exc


def copy_workspace_for_staging(source: Path, destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)

    def ignore(current: str, names: list[str]) -> set[str]:
        current_path = Path(current)
        ignored: set[str] = set()
        for name in names:
            full = current_path / name
            rel = full.relative_to(source)
            if full.is_symlink() or name in EXCLUDED_DIR_NAMES or is_secret_relative(rel):
                ignored.add(name)
        return ignored

    shutil.copytree(source, destination, symlinks=True, ignore=ignore)


def diff_snapshots(before: dict[str, FileRecord], after: dict[str, FileRecord]) -> dict[str, list[str]]:
    before_keys = set(before)
    after_keys = set(after)
    return {
        "added": sorted(after_keys - before_keys),
        "deleted": sorted(before_keys - after_keys),
        "modified": sorted(path for path in before_keys & after_keys if before[path] != after[path]),
    }


def parse_scope(text: str) -> tuple[list[str], str, list[str]]:
    matches = SCOPE_RE.findall(text)
    if not matches:
        raise RunnerError("scope_missing", "Codex analysis did not return a <codex_scope> block")
    try:
        payload = json.loads(matches[-1])
    except json.JSONDecodeError as exc:
        raise RunnerError("scope_invalid", "Codex scope JSON was invalid") from exc
    if not isinstance(payload, dict):
        raise RunnerError("scope_invalid", "Codex scope must be a JSON object")
    raw_paths = payload.get("paths")
    if not isinstance(raw_paths, list) or not raw_paths or len(raw_paths) > 30:
        raise RunnerError("scope_invalid", "Codex scope must contain 1-30 relative file paths")
    paths: list[str] = []
    for raw in raw_paths:
        if not isinstance(raw, str):
            raise RunnerError("scope_invalid", "scope paths must be strings")
        candidate = raw.strip().replace("\\", "/")
        rel = Path(candidate)
        if not candidate or rel.is_absolute() or ".." in rel.parts or candidate.endswith("/"):
            raise RunnerError("scope_invalid", f"unsafe scope path: {raw!r}")
        normalized = rel.as_posix()
        if normalized not in paths:
            paths.append(normalized)
    summary = str(payload.get("summary") or "").strip()[:1_000]
    raw_tests = payload.get("tests") or []
    tests = [str(item).strip()[:500] for item in raw_tests if str(item).strip()][:10] if isinstance(raw_tests, list) else []
    return paths, summary, tests


def secret_output_reason(text: str) -> str | None:
    return SecretScanner().scan_text(text)


def _secret_targets(workspace: Path) -> tuple[list[Path], list[Path]]:
    files: list[Path] = []
    dirs: list[Path] = []
    for current, dirnames, filenames in os.walk(workspace, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(dirnames):
            rel = (current_path / name).relative_to(workspace)
            full = current_path / name
            if name in EXCLUDED_DIR_NAMES or is_secret_relative(rel):
                # A symlinked directory is not traversed and cannot reach its
                # original absolute target because the host root is absent in
                # the sandbox.  Do not ask bwrap to mount over a symlink.
                if not full.is_symlink():
                    dirs.append(rel)
                dirnames.remove(name)
        for name in filenames:
            rel = (current_path / name).relative_to(workspace)
            full = current_path / name
            if is_secret_relative(rel) and not full.is_symlink():
                files.append(rel)
    return files, dirs


def _append_readonly_file(argv: list[str], source: Path, destination: str) -> None:
    if source.is_file():
        argv.extend(["--ro-bind", str(source), destination])


def build_bwrap_argv(workspace: Path, write: bool, auth_fd: int = 3) -> list[str]:
    """Build the isolated Codex command.

    ``auth_fd`` must refer to a sealed memfd inherited by bubblewrap.  It is
    copied into the sandbox tmpfs and consumed by ``auth_wrapper.py``.
    The default keeps the historical two-argument call useful for inspection,
    but an argv built with the default is runnable only when FD 3 is supplied.
    """
    try:
        codex_real = CODEX_BIN.resolve(strict=True)
        workspace_real = workspace.resolve(strict=True)
    except OSError as exc:
        raise RunnerError("path_unavailable", "Codex or workspace path is unavailable") from exc
    if not codex_real.is_file() or not os.access(codex_real, os.X_OK):
        raise RunnerError("codex_missing", "Codex CLI is unavailable")
    if not BWRAP_BIN.is_file() or not os.access(BWRAP_BIN, os.X_OK):
        raise RunnerError("bwrap_missing", "bubblewrap is unavailable")
    if not AUTH_WRAPPER.is_file():
        raise RunnerError("auth_wrapper_missing", "Codex authentication wrapper is unavailable")
    if not workspace_real.is_dir():
        raise RunnerError("workspace_invalid", "Codex workspace is not a directory")
    if auth_fd < 0:
        raise RunnerError("codex_auth_fd_missing", "Codex authentication descriptor is invalid")

    argv = [
        str(BWRAP_BIN),
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--unshare-user",
        # WSL cannot initialize loopback in an unshared network namespace.
        # The Codex parent needs HTTPS, while the mandatory legacy Landlock
        # sandbox below denies socket creation to every model-generated tool.
        "--share-net",
        "--disable-userns",
        "--cap-drop",
        "ALL",
        "--hostname",
        "hermes-codex",
        "--clearenv",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/sbin",
        "/sbin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--dir",
        "/etc",
        "--dir",
        "/etc/ssl",
        "--ro-bind",
        "/etc/ssl/certs",
        "/etc/ssl/certs",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/run",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/home",
        "--dir",
        "/home/codex",
        "--dir",
        SANDBOX_CODEX_HOME,
        "--dir",
        "/opt",
        "--dir",
        "/opt/codex",
        "--ro-bind",
        str(codex_real),
        SANDBOX_CODEX_BIN,
        "--ro-bind",
        str(AUTH_WRAPPER),
        SANDBOX_AUTH_WRAPPER,
        "--dir",
        "/work",
        "--bind" if write else "--ro-bind",
        str(workspace_real),
        "/work",
    ]

    # Only public TLS/DNS configuration is exposed.  In particular there is
    # no bind of /, /opt/data, /home/hermes, or the real CODEX_HOME.
    for source, destination in (
        (Path("/etc/ssl/openssl.cnf"), "/etc/ssl/openssl.cnf"),
        (Path("/etc/resolv.conf"), "/etc/resolv.conf"),
        (Path("/etc/hosts"), "/etc/hosts"),
        (Path("/etc/nsswitch.conf"), "/etc/nsswitch.conf"),
        (Path("/etc/gai.conf"), "/etc/gai.conf"),
        (Path("/etc/services"), "/etc/services"),
        (Path("/etc/protocols"), "/etc/protocols"),
        (Path("/etc/localtime"), "/etc/localtime"),
    ):
        _append_readonly_file(argv, source, destination)

    secret_files, secret_dirs = _secret_targets(workspace_real)
    for rel in sorted(secret_dirs, key=lambda item: len(item.parts)):
        destination = f"/work/{rel.as_posix()}"
        argv.extend(["--tmpfs", destination, "--remount-ro", destination])
    for rel in secret_files:
        argv.extend(["--ro-bind", "/dev/null", f"/work/{rel.as_posix()}"])

    argv.extend(
        [
            "--perms",
            "0600",
            "--file",
            str(auth_fd),
            SANDBOX_AUTH_SEED,
            "--chdir",
            "/work",
            "--setenv",
            "HOME",
            "/home/codex",
            "--setenv",
            "CODEX_HOME",
            SANDBOX_CODEX_HOME,
            "--setenv",
            "CODEX_SQLITE_HOME",
            "/tmp/codex-sqlite",
            "--setenv",
            "PATH",
            "/usr/bin:/bin",
            "--setenv",
            "LANG",
            "C.UTF-8",
            "--setenv",
            "LC_ALL",
            "C.UTF-8",
            "--setenv",
            "TMPDIR",
            "/tmp",
            "--setenv",
            "TERM",
            "dumb",
            "--setenv",
            "TZ",
            "Asia/Seoul",
            "--setenv",
            "SSL_CERT_FILE",
            "/etc/ssl/certs/ca-certificates.crt",
            "--setenv",
            "PYTHONNOUSERSITE",
            "1",
            "--setenv",
            "GIT_CONFIG_NOSYSTEM",
            "1",
            "--setenv",
            "GIT_CONFIG_GLOBAL",
            "/dev/null",
            "/usr/bin/python3",
            SANDBOX_AUTH_WRAPPER,
            SANDBOX_CODEX_BIN,
            "-a",
            "never",
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--strict-config",
            "--enable",
            "use_legacy_landlock",
            "--disable",
            "apps",
            "--disable",
            "browser_use",
            "--disable",
            "browser_use_external",
            "--disable",
            "computer_use",
            "--disable",
            "image_generation",
            "--disable",
            "multi_agent",
            "--disable",
            "plugins",
            "--disable",
            "memories",
            "--disable",
            "standalone_web_search",
            "--disable",
            "web_search_request",
            "--disable",
            "web_search_cached",
            "--disable",
            "in_app_browser",
            "-m",
            MODEL,
            "-c",
            "check_for_update_on_startup=false",
            "-c",
            f'model_reasoning_effort="{"high" if write else "medium"}"',
            "-c",
            'shell_environment_policy.inherit="none"',
            "-c",
            'shell_environment_policy.set={PATH="/usr/bin:/bin",HOME="/tmp",LANG="C.UTF-8",LC_ALL="C.UTF-8",PYTHONNOUSERSITE="1",GIT_CONFIG_NOSYSTEM="1",GIT_CONFIG_GLOBAL="/dev/null"}',
            "-c",
            "sandbox_workspace_write.writable_roots=[]",
            "-c",
            "sandbox_workspace_write.network_access=false",
            "-c",
            "sandbox_workspace_write.exclude_tmpdir_env_var=true",
            "-c",
            "sandbox_workspace_write.exclude_slash_tmp=true",
            "-s",
            "workspace-write" if write else "read-only",
            "-C",
            "/work",
            "--skip-git-repo-check",
            "--color",
            "never",
            "--json",
            "-",
        ]
    )
    return argv


def _create_auth_memfd() -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        source_fd = os.open(AUTH_PATH, flags)
    except OSError as exc:
        raise RunnerError("codex_auth_missing", "Codex authentication is unavailable") from exc
    payload = bytearray()
    try:
        info = os.fstat(source_fd)
        if not stat.S_ISREG(info.st_mode) or not (0 < info.st_size <= MAX_AUTH_BYTES):
            raise RunnerError("codex_auth_invalid", "Codex authentication has an invalid type or size")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise RunnerError("codex_auth_permissions", "Codex authentication permissions are too broad")
        remaining = MAX_AUTH_BYTES + 1
        while remaining > 0:
            chunk = os.read(source_fd, min(64 * 1024, remaining))
            if not chunk:
                break
            payload.extend(chunk)
            remaining -= len(chunk)
        if not payload or len(payload) > MAX_AUTH_BYTES:
            raise RunnerError("codex_auth_invalid", "Codex authentication has an invalid size")
    finally:
        os.close(source_fd)

    if not hasattr(os, "memfd_create") or not hasattr(os, "MFD_ALLOW_SEALING"):
        for index in range(len(payload)):
            payload[index] = 0
        raise RunnerError("memfd_unavailable", "sealed in-memory authentication is unavailable")

    memfd = -1
    try:
        memfd = os.memfd_create(
            "hermes-codex-auth",
            flags=os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
        )
        os.fchmod(memfd, 0o600)
        view = memoryview(payload)
        written = 0
        while written < len(view):
            count = os.write(memfd, view[written:])
            if count <= 0:
                raise OSError("short write to authentication memfd")
            written += count
        os.lseek(memfd, 0, os.SEEK_SET)
        seal_flags = (
            fcntl.F_SEAL_SEAL
            | fcntl.F_SEAL_SHRINK
            | fcntl.F_SEAL_GROW
            | fcntl.F_SEAL_WRITE
        )
        fcntl.fcntl(memfd, fcntl.F_ADD_SEALS, seal_flags)
        return memfd
    except BaseException as exc:
        if memfd >= 0:
            os.close(memfd)
        if isinstance(exc, RunnerError):
            raise
        raise RunnerError("memfd_failed", "failed to prepare in-memory authentication") from exc
    finally:
        for index in range(len(payload)):
            payload[index] = 0


@dataclass(frozen=True)
class _CapturedStream:
    data: bytes
    overflow: bool


async def _read_limited(
    reader: asyncio.StreamReader,
    limit: int,
    overflow_event: asyncio.Event,
) -> _CapturedStream:
    """Drain a stream completely while retaining at most ``limit`` bytes."""
    result = bytearray()
    total = 0
    overflow = False
    while True:
        chunk = await reader.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if len(result) < limit:
            result.extend(chunk[: limit - len(result)])
        if total > limit and not overflow:
            overflow = True
            overflow_event.set()
    return _CapturedStream(bytes(result), overflow)


async def _feed_stdin(writer: asyncio.StreamWriter, payload: bytes) -> None:
    try:
        writer.write(payload)
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


def _parse_jsonl(stdout: str) -> tuple[str, str | None, str | None]:
    final_text = ""
    thread_id: str | None = None
    failure: str | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = str(event.get("thread_id") or "") or None
        elif event_type == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                final_text = item["text"]
        elif event_type in {"turn.failed", "error"}:
            failure = str(event.get("error") or event.get("message") or "Codex turn failed")[:500]
    return final_text, thread_id, failure


class CodexRunner:
    def __init__(self, scanner: SecretScanner | None = None) -> None:
        self.scanner = scanner if scanner is not None else SecretScanner.from_known_sources()
        self._active: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._terminate_lock = asyncio.Lock()

    async def cancel_active(self) -> bool:
        process = self._active
        if process is None or process.returncode is not None:
            return False
        await self._terminate(process)
        return True

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        async with self._terminate_lock:
            if process.returncode is not None:
                return
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                await process.wait()
                return
            try:
                await asyncio.wait_for(process.wait(), timeout=TERMINATE_GRACE_SECONDS)
                return
            except asyncio.TimeoutError:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()

    async def _supervise(
        self,
        process: asyncio.subprocess.Process,
        encoded_prompt: bytes,
        stdout_task: "asyncio.Task[_CapturedStream]",
        stderr_task: "asyncio.Task[_CapturedStream]",
        overflow_event: asyncio.Event,
    ) -> tuple[_CapturedStream, _CapturedStream]:
        assert process.stdin is not None
        feed_task = asyncio.create_task(_feed_stdin(process.stdin, encoded_prompt))
        wait_task = asyncio.create_task(process.wait())
        overflow_task = asyncio.create_task(overflow_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {wait_task, overflow_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if overflow_task in done and overflow_event.is_set() and process.returncode is None:
                await self._terminate(process)
            else:
                await wait_task
            await feed_task
            stdout_capture, stderr_capture = await asyncio.gather(stdout_task, stderr_task)
            return stdout_capture, stderr_capture
        finally:
            for task in (feed_task, wait_task, overflow_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(feed_task, wait_task, overflow_task, return_exceptions=True)

    async def run(self, workspace: Path, prompt: str, write: bool) -> CodexResult:
        encoded = prompt.encode("utf-8")
        if len(encoded) > MAX_PROMPT_BYTES:
            raise RunnerError("prompt_limit", "Codex prompt exceeded the configured limit")
        input_reason = self.scanner.scan_bytes(encoded)
        if input_reason:
            raise RunnerError("secret_input_blocked", f"Codex input matched secret source: {input_reason}")
        async with self._lock:
            auth_fd = _create_auth_memfd()
            try:
                argv = build_bwrap_argv(workspace, write=write, auth_fd=auth_fd)
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                    pass_fds=(auth_fd,),
                )
            finally:
                os.close(auth_fd)
            self._active = process
            assert process.stdin and process.stdout and process.stderr
            overflow_event = asyncio.Event()
            stdout_task = asyncio.create_task(_read_limited(process.stdout, MAX_STDOUT_BYTES, overflow_event))
            stderr_task = asyncio.create_task(_read_limited(process.stderr, MAX_STDERR_BYTES, overflow_event))
            try:
                stdout_capture, stderr_capture = await asyncio.wait_for(
                    self._supervise(process, encoded, stdout_task, stderr_task, overflow_event),
                    timeout=RUN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError as exc:
                await self._terminate(process)
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                raise RunnerError("timeout", "Codex exceeded the 300 second timeout") from exc
            except BaseException:
                await self._terminate(process)
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                raise
            finally:
                self._active = None

        if stdout_capture.overflow or stderr_capture.overflow:
            raise RunnerError("output_limit", "Codex output exceeded the configured limit")
        for stream_name, captured in (("stdout", stdout_capture.data), ("stderr", stderr_capture.data)):
            reason = self.scanner.scan_bytes(captured)
            if reason:
                raise RunnerError(
                    "secret_output_blocked",
                    f"Codex {stream_name} matched secret source: {reason}",
                )

        stdout = stdout_capture.data.decode("utf-8", errors="replace")
        stderr_tail = stderr_capture.data[-STDERR_TAIL_BYTES:].decode("utf-8", errors="replace")
        final_text, thread_id, failure = _parse_jsonl(stdout)
        if process.returncode != 0:
            raise RunnerError("codex_exit", f"Codex exited with status {process.returncode}")
        if failure:
            raise RunnerError("codex_failed", failure)
        if not final_text.strip():
            raise RunnerError("empty_result", "Codex returned no final agent message")
        final_text = final_text.strip()[:MAX_RESULT_CHARS]
        reason = self.scanner.scan_text(final_text)
        if reason:
            raise RunnerError("secret_output_blocked", f"Codex output matched secret pattern: {reason}")
        return CodexResult(final_text, thread_id, int(process.returncode or 0), stderr_tail)

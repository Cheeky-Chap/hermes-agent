"""Deliver Codex authentication once, without exposing it on disk or argv.

This module runs as PID 1 inside the outer bubblewrap sandbox.  Bubblewrap
copies a sealed, inherited memfd to ``AUTH_SEED_PATH`` on the sandbox's
tmpfs.  The wrapper removes that seed before starting Codex and serves the
same bytes through a one-shot FIFO at ``$CODEX_HOME/auth.json``.

The FIFO is unlinked as soon as Codex opens it, so commands subsequently
spawned by Codex cannot reopen the credential file.
"""

from __future__ import annotations

import ctypes
import os
import signal
import stat
import sys
from pathlib import Path


AUTH_SEED_PATH = Path("/run/codex-auth-seed")
MAX_AUTH_BYTES = 128 * 1024
AUTH_DELIVERY_TIMEOUT_SECONDS = 20
PR_SET_PDEATHSIG = 1


def _fatal(message: str) -> "NoReturn":
    print(f"auth wrapper: {message}", file=sys.stderr)
    raise SystemExit(74)


def _read_seed() -> bytearray:
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(AUTH_SEED_PATH, flags)
    except OSError:
        _fatal("authentication seed is unavailable")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not (0 < info.st_size <= MAX_AUTH_BYTES):
            _fatal("authentication seed has an invalid type or size")
        chunks: list[bytes] = []
        remaining = MAX_AUTH_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = bytearray(b"".join(chunks))
        if not payload or len(payload) > MAX_AUTH_BYTES:
            _fatal("authentication seed has an invalid size")
        return payload
    finally:
        os.close(fd)
        try:
            AUTH_SEED_PATH.unlink()
        except FileNotFoundError:
            pass


def _set_parent_death_signal() -> None:
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl failed")
    except (AttributeError, OSError):
        # The alarm below still bounds a blocked FIFO writer.  This process is
        # also in Codex's process group and the outer bwrap PID namespace.
        pass


def _writer(fifo: Path, payload: bytearray) -> "NoReturn":
    _set_parent_death_signal()

    def timed_out(_signum: int, _frame: object) -> None:
        raise TimeoutError

    signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(AUTH_DELIVERY_TIMEOUT_SECONDS)
    fd = -1
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_CLOEXEC)
        # The reader already holds the FIFO at this point.  Removing the name
        # before writing eliminates the window for a second opener.
        try:
            fifo.unlink()
        except FileNotFoundError:
            pass
        view = memoryview(payload)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
    except (BrokenPipeError, OSError, TimeoutError):
        pass
    finally:
        signal.alarm(0)
        if fd >= 0:
            os.close(fd)
        for index in range(len(payload)):
            payload[index] = 0
        try:
            fifo.unlink()
        except FileNotFoundError:
            pass
        os._exit(0)


def main() -> int:
    if len(sys.argv) < 2:
        _fatal("missing Codex command")
    codex_home_value = os.environ.get("CODEX_HOME", "").strip()
    if not codex_home_value:
        _fatal("CODEX_HOME is not set")
    codex_home = Path(codex_home_value)
    codex_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(codex_home, 0o700)
    auth_fifo = codex_home / "auth.json"
    try:
        auth_fifo.unlink()
    except FileNotFoundError:
        pass

    payload = _read_seed()
    os.mkfifo(auth_fifo, 0o600)
    child = os.fork()
    if child == 0:
        _writer(auth_fifo, payload)

    # The writer has a copy-on-write copy.  Erase the parent's copy before
    # replacing this process with Codex.
    for index in range(len(payload)):
        payload[index] = 0
    del payload
    try:
        os.execv(sys.argv[1], sys.argv[1:])
    except OSError:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _fatal("failed to execute Codex")


if __name__ == "__main__":
    raise SystemExit(main())

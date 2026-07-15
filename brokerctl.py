#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket

from common import JOB_ID_RE, SOCKET_PATH


def request(payload: dict) -> dict:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    try:
        client.connect(str(SOCKET_PATH))
        client.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        buffer = bytearray()
        while b"\n" not in buffer:
            chunk = client.recv(64 * 1024)
            if not chunk:
                break
            buffer.extend(chunk)
            if len(buffer) > 512 * 1024:
                raise RuntimeError("response too large")
        return json.loads(bytes(buffer).split(b"\n", 1)[0].decode())
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Local Hermes Codex broker status client")
    parser.add_argument("command", choices=("ping", "status"))
    parser.add_argument("job_id", nargs="?")
    args = parser.parse_args()
    if args.command == "ping":
        payload = {"op": "ping"}
    else:
        job_id = str(args.job_id or "").upper()
        if not JOB_ID_RE.fullmatch(job_id):
            parser.error("status requires a valid HC-YYYYMMDD-ABCD job id")
        payload = {"op": "get", "job_id": job_id}
    try:
        response = request(payload)
    except (OSError, ValueError, json.JSONDecodeError):
        print("error: broker_unavailable")
        return 1
    if not response.get("ok"):
        error = response.get("error") or {}
        print(f"error: {error.get('code', 'unknown')}")
        return 1
    result = response.get("result") or {}
    if args.command == "ping":
        print(f"status={result.get('status')} active={result.get('active_job') or '-'} queued={result.get('queued', 0)}")
    else:
        print(f"id={result.get('id')} status={result.get('status')} workspace={result.get('workspace')}")
        if result.get("scope_hash"):
            print(f"scope_hash={result['scope_hash']}")
        if result.get("changed_paths"):
            print("changed_paths=" + ",".join(result["changed_paths"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

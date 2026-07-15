#!/usr/bin/env bash
set -euo pipefail
umask 0077

SERVICE_DIR="/opt/data/services/hermes-codex-broker"
CHECK_TMP="/opt/data/state/hermes-codex-broker/test-tmp"

if [[ "${1:-}" == "--check" ]]; then
  mkdir -p "$CHECK_TMP"
  chmod 0700 "$CHECK_TMP"
  check_files=(
    "$SERVICE_DIR/common.py"
    "$SERVICE_DIR/codex_runner.py"
    "$SERVICE_DIR/broker.py"
    "$SERVICE_DIR/brokerctl.py"
    "$SERVICE_DIR/launch_container.py"
    "$SERVICE_DIR/discord_audit_check.py"
    "$SERVICE_DIR/hermes_plugin/__init__.py"
  )
  PYTHONDONTWRITEBYTECODE=1 python3 -B -c \
    'import ast,pathlib,sys; [ast.parse(pathlib.Path(p).read_text(encoding="utf-8"), filename=p) for p in sys.argv[1:]]' \
    "${check_files[@]}"
  TMPDIR="$CHECK_TMP" PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover \
    -s "$SERVICE_DIR/tests" -p 'test_*.py'
  PYTHONDONTWRITEBYTECODE=1 python3 -B "$SERVICE_DIR/broker.py" --check
  PYTHONDONTWRITEBYTECODE=1 python3 -B "$SERVICE_DIR/launch_container.py" --check
  exit 0
fi

exec env PYTHONDONTWRITEBYTECODE=1 python3 -B "$SERVICE_DIR/broker.py"

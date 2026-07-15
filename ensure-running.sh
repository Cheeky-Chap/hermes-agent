#!/usr/bin/env bash
set -euo pipefail
umask 0077

SERVICE_DIR="/opt/data/services/hermes-codex-broker"
DISABLED="/opt/data/state/hermes-codex-broker/DISABLED"

[[ -e "$DISABLED" ]] && exit 0
if tmux has-session -t hermes-codex-broker 2>/dev/null; then
  exit 0
fi
exec "$SERVICE_DIR/run-background.sh"

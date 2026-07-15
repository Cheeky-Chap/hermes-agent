#!/usr/bin/env bash
set -euo pipefail
umask 0077

SERVICE_DIR="/opt/data/services/hermes-codex-broker"
SESSION="hermes-codex-broker"
DISABLED="/opt/data/state/hermes-codex-broker/DISABLED"

if [[ -e "$DISABLED" ]]; then
  echo "broker is disabled by $DISABLED"
  exit 1
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "broker already running in tmux session $SESSION"
  exit 0
fi

tmux new-session -d -s "$SESSION" \
  "umask 0077; exec env PYTHONDONTWRITEBYTECODE=1 python3 -B '$SERVICE_DIR/broker.py'"
sleep 1
PYTHONDONTWRITEBYTECODE=1 python3 -B "$SERVICE_DIR/brokerctl.py" ping

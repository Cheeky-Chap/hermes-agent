#!/usr/bin/env bash
set -euo pipefail
umask 0077

DISABLED="/opt/data/state/hermes-codex-broker/DISABLED"
touch "$DISABLED"
chmod 600 "$DISABLED"
if tmux has-session -t hermes-codex-broker 2>/dev/null; then
  tmux send-keys -t hermes-codex-broker C-c
fi
echo "broker disabled; the Hermes container was not modified"

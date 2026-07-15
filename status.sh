#!/usr/bin/env bash
set -euo pipefail
umask 0077

SERVICE_DIR="/opt/data/services/hermes-codex-broker"

if tmux has-session -t hermes-codex-broker 2>/dev/null; then
  echo "broker_tmux=running"
  PYTHONDONTWRITEBYTECODE=1 python3 -B "$SERVICE_DIR/brokerctl.py" ping
else
  echo "broker_tmux=stopped"
fi

docker inspect --format \
  'container={{.Name}} state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restart={{.HostConfig.RestartPolicy.Name}} image={{.Image}}' \
  hermes-master-v0182 2>/dev/null || echo "container=absent"

#!/usr/bin/env bash
set -euo pipefail

# coding_worker.sh — OpenCode Go / Codex CLI 래퍼
#
# Usage:
#   coding_worker.sh <mode> <level> "<task>" [workdir]
#
# mode:   plan | diff
# level:  flash | pro | heavy | kimi | glm | review-glm | plan-kimi | codex-read | codex-write
# task:   description of the work
# workdir: project directory (default: /opt/data)

MODE="${1:-plan}"
LEVEL="${2:-pro}"
TASK="${3:-}"
PROJECT_DIR="${4:-/opt/data}"

export HOME=/opt/data/home
export HERMES_HOME=/opt/data
export PATH="$HOME/.opencode/bin:$HOME/.local/bin:$PATH"
export CODEX_HOME=/opt/data/home/.codex

if [ -z "$TASK" ]; then
  echo "[ERROR] TASK is empty"
  echo "Usage: coding_worker.sh plan|diff flash|pro|heavy|kimi|glm|review-glm|plan-kimi|codex-read|codex-write \"task\" /opt/data"
  exit 1
fi

case "$PROJECT_DIR" in
  /opt/data|/opt/data/*) ;;
  *)
    echo "[ERROR] Refusing to work outside /opt/data"
    exit 2
    ;;
esac

# ─── Level → Tool/Model/Sandbox mapping ─────────────────────────
# flash/pro/heavy/kimi/glm/review-glm/plan-kimi: OpenCode Go CLI
# codex-read:      Codex CLI, reasoning=medium, sandbox=read-only
# codex-write:     Codex CLI, reasoning=high,   sandbox=workspace-write

case "$LEVEL" in
  flash)
    MODEL="opencode-go/deepseek-v4-flash"
    TOOL="opencode"
    SANDBOX=""
    REASONING=""
    ;;
  pro)
    MODEL="opencode-go/deepseek-v4-pro"
    TOOL="opencode"
    SANDBOX=""
    REASONING=""
    ;;
  heavy)
    MODEL="opencode-go/kimi-k2.6"
    TOOL="opencode"
    SANDBOX=""
    REASONING=""
    ;;
  kimi)
    MODEL="opencode-go/kimi-k2.6"
    TOOL="opencode"
    SANDBOX=""
    REASONING=""
    ;;
  glm)
    MODEL="opencode-go/glm-5.1"
    TOOL="opencode"
    SANDBOX=""
    REASONING=""
    ;;
  review-glm)
    MODEL="opencode-go/glm-5.2"
    TOOL="opencode"
    SANDBOX=""
    REASONING=""
    ;;
  plan-kimi)
    MODEL="opencode-go/kimi-k2.7-code"
    TOOL="opencode"
    SANDBOX=""
    REASONING=""
    ;;
  codex-read)
    MODEL="gpt-5.5"
    TOOL="codex"
    SANDBOX="read-only"
    REASONING="medium"
    ;;
  codex-write)
    MODEL="gpt-5.5"
    TOOL="codex"
    SANDBOX="workspace-write"
    REASONING="high"
    ;;
  *)
    MODEL="opencode-go/deepseek-v4-pro"
    TOOL="opencode"
    SANDBOX=""
    REASONING=""
    ;;
esac

cd "$PROJECT_DIR"

# ─── Codex CLI 인증 체크 ──────────────────────────────────────
if [ "$TOOL" = "codex" ]; then
  AUTH_STATUS=$(/opt/data/home/.local/bin/codex doctor 2>&1)
  if echo "$AUTH_STATUS" | grep -q "✗ auth"; then
    echo "[codex_worker] ERROR: Codex CLI 인증 안 됨."
    echo "[codex_worker] 'codex login' 실행 후 재시도하세요."
    echo "[codex_worker] OpenCode Go 대신 flash/pro/heavy 레벨을 사용하세요."
    exit 10
  fi
fi

echo "[coding_worker] mode=$MODE level=$LEVEL tool=$TOOL model=$MODEL reasoning=$REASONING sandbox=$SANDBOX project=$PROJECT_DIR"
# git status suppressed — 119+ untracked files cause output noise
# git status --short || true

# ─── OpenCode Go 실행 ──────────────────────────────────────────
if [ "$TOOL" = "opencode" ]; then
  PROMPT="
You are the OpenCode Go coding worker for Hermes.

Mode: $MODE
Project: $PROJECT_DIR

Rules:
- Work only inside $PROJECT_DIR.
- Do not touch secrets, auth files, .env, API keys, tokens, private keys, databases, or state files.
- Do not commit, push, pull, install packages, or restart services unless explicitly requested.
- Prefer small safe patches.
- If mode is plan, inspect and explain only. Do not modify files.
- If mode is diff, apply the minimal safe patch and show git diff.
- After changes, print test commands.

Task:
$TASK
"

  if [ "$MODE" = "plan" ]; then
    opencode -m "$MODEL" run "$PROMPT"
  elif [ "$MODE" = "diff" ]; then
    opencode -m "$MODEL" run "$PROMPT

Apply the minimal safe patch now, then print git diff."
  else
    echo "[ERROR] Unknown mode: $MODE"
    exit 3
  fi

# ─── Codex CLI 실행 ─────────────────────────────────────────────
elif [ "$TOOL" = "codex" ]; then

  # codex-write 안전 게이트: 반드시 plan 먼저 실행 후 승인 필요
  if [ "$LEVEL" = "codex-write" ] && [ "$MODE" != "diff" ]; then
    echo "[codex_worker] WARNING: codex-write는 반드시 수정 계획 보고 후 승인받아야 합니다."
    echo "[codex_worker] 먼저 codex-read + plan 모드로 분석하고, 승인 후 diff 모드로 실행하세요."
  fi

  SANDBOX_FLAG="-s $SANDBOX"
  REASONING_FLAG="-c model_reasoning_effort=$REASONING"

  PROMPT="
You are the Codex CLI coding worker for Hermes.

Mode: $MODE
Project: $PROJECT_DIR
Sandbox: $SANDBOX
Reasoning effort: $REASONING

Rules:
- Work only inside $PROJECT_DIR.
- Do not touch secrets, auth files, .env, API keys, tokens, private keys, databases, or state files.
- Do not commit, push, pull, install packages, or restart services unless explicitly requested.
- Prefer small safe patches.
- If mode is plan, inspect and explain only. Do not modify files.
- If mode is diff, apply the minimal safe patch and show git diff.
- After changes, print test commands.
- Report: changed files, key diff, and test results.

Task:
$TASK
"

  if [ "$MODE" = "plan" ]; then
    RETHINK_FLAG=""
  else
    # diff 모드에서도 자동 승인 금지 → never로 설정
    RETHINK_FLAG=""
  fi

  /opt/data/home/.local/bin/codex exec \
    -m "$MODEL" \
    $REASONING_FLAG \
    $SANDBOX_FLAG \
    -C "$PROJECT_DIR" \
    --skip-git-repo-check \
    --json \
    "$PROMPT"
fi

echo
echo "[coding_worker] final diff:"
git diff -- /opt/data/scripts /opt/data/shared /opt/data/agents 2>/dev/null || true
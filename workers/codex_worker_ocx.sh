#!/usr/bin/env bash
set -euo pipefail

# codex_worker_ocx.sh — opencodex 경유 Codex CLI 래퍼
#
# opencodex 프록시(localhost:10100)를 통해 Codex CLI가 다양한 LLM을
# 사용할 수 있게 합니다. 기존 coding_worker.sh/codex_worker.sh의
# fallback을 유지하면서 opencodex 기반 라우팅을 추가합니다.
#
# Prerequisites:
#   npm install -g @bitkyc08/opencodex --prefix=$HOME/.local
#   ocx start  (or systemd service: ocx service install)
#   OPENCODE_API_KEY in .env or config
#
# Usage:
#   codex_worker_ocx.sh read|write <reasoning> <workdir> <model> "<task>"
#
# Arguments:
#   task_type:  read (=read-only 분석) | write (=workspace-write 수정)
#   reasoning:  medium (기본) | high (코드 수정, 보안, 리스크 로직)
#   workdir:    /opt/data/{scripts,agents,shared,state}
#   model:      opencode-go/deepseek-v4-pro | opencode-go/glm-5.1 | ...
#   task:       작업 설명
#
# Models available via opencodex (localhost:10100):
#   opencode-go/deepseek-v4-flash   — 빠른 분석
#   opencode-go/deepseek-v4-pro     — 심층 분석/코드 리뷰
#   opencode-go/glm-5.1             — 일반 코드 작업
#   opencode-go/glm-5.2             — 리뷰 전용
#   opencode-go/kimi-k2.6           — 코드 생성
#   opencode-go/kimi-k2.7-code      — 계획/설계
#   opencode-go/mimo-v2.5           — 이미지/비전
#   opencode-go/minimax-m3          — 최신 모델
#   gpt-5.4-mini                    — 빠른 GPT (ChatGPT auth 필요)
#
# Sandbox:
#   read  → read-only (분석만, 파일 수정 불가)
#   write → workspace-write (파일 수정 가능, reasoning=high 강제)

TASK_TYPE="${1:-read}"
REASONING="${2:-medium}"
WORKDIR="${3:-/opt/data/scripts}"
MODEL="${4:-opencode-go/deepseek-v4-flash}"
TASK="${5:-}"

export HOME=/opt/data/home
export CODEX_HOME="$HOME/.codex"
export PATH="$HOME/.local/bin:$PATH"

# ─── 인수 검증 ────────────────────────────────────────────────
if [ -z "$TASK" ]; then
  echo "[codex_worker_ocx] ERROR: TASK is empty"
  echo "Usage: codex_worker_ocx.sh read|write medium|high /opt/data/scripts model \"task\""
  exit 1
fi

# ─── 작업 디렉터리 제한 ────────────────────────────────────────
case "$WORKDIR" in
  /opt/data/scripts|/opt/data/scripts/*) ;;
  /opt/data/agents|/opt/data/agents/*) ;;
  /opt/data/shared|/opt/data/shared/*) ;;
  /opt/data/state|/opt/data/state/*) ;;
  *)
    echo "[codex_worker_ocx] ERROR: workdir must be under /opt/data/{scripts,agents,shared,state}"
    echo "  Provided: $WORKDIR"
    exit 3
    ;;
esac

# ─── 타입 → sandbox 매핑 ──────────────────────────────────────
case "$TASK_TYPE" in
  read)
    SANDBOX="read-only"
    ;;
  write)
    SANDBOX="workspace-write"
    REASONING="high"  # write는 항상 high reasoning 강제
    ;;
  *)
    echo "[codex_worker_ocx] ERROR: Unknown TASK_TYPE: $TASK_TYPE (use 'read' or 'write')"
    exit 2
    ;;
esac

# ─── opencodex 프록시 상태 확인 ──────────────────────────────
if ! curl -sf http://localhost:10100/healthz > /dev/null 2>&1; then
  echo "[codex_worker_ocx] ⚠ opencodex proxy not running at localhost:10100"
  echo "[codex_worker_ocx] Starting proxy..."
  export OPENCODE_API_KEY
  ocx start &
  # 15초 대기
  for i in $(seq 15); do
    sleep 1
    if curl -sf http://localhost:10100/healthz > /dev/null 2>&1; then
      echo "[codex_worker_ocx] ✅ opencodex proxy started"
      break
    fi
    if [ "$i" -eq 15 ]; then
      echo "[codex_worker_ocx] ❌ opencodex proxy failed to start"
      echo "[codex_worker_ocx] Fallback to codex_worker.sh or coding_worker.sh"
      exit 11
    fi
  done
fi

# ─── Codex CLI 실행 ─────────────────────────────────────────────
echo "[codex_worker_ocx] type=$TASK_TYPE reasoning=$REASONING sandbox=$SANDBOX model=$MODEL workdir=$WORKDIR"
echo "[codex_worker_ocx] task: $TASK"

cd "$WORKDIR"

/opt/data/home/.local/bin/codex exec \
  -m "$MODEL" \
  -c "model_reasoning_effort=$REASONING" \
  -s "$SANDBOX" \
  -C "$WORKDIR" \
  --skip-git-repo-check \
  --json \
  "$TASK"

echo
echo "[codex_worker_ocx] 완료. 변경 사항 확인:"
git diff --stat 2>/dev/null || true

#!/usr/bin/env bash
set -euo pipefail

# codex_worker.sh — Codex CLI (gpt-5.5) 래퍼
#
# Usage:
#   codex_worker.sh read|write medium|high <workdir> "<task>"
#
# task_type:  read (=read-only 분석) | write (=workspace-write 수정)
# reasoning:  medium (기본) | high (코드 수정, 보안, 주식봇 리스크, DB)
# workdir:    /opt/data/scripts 등 (제한된 범위)
# task:       작업 설명
#
# 안전 규칙:
#   - read 타입은 항상 sandbox=read-only로 실행
#   - write 타입은 sandbox=workspace-write, reasoning=high 필수
#   - write 금지 조건: 수정 계획 보고 전에는 workspace-write 실행 불가
#   - 작업 디렉터리 제한: /opt/data/{scripts,agents,shared} 하위만 허용
#   - 인증 안 됨: codex login 필요 메시지 출력 후 종료
#   - /opt/data/auth.json 토큰 강제 주입 금지
#
# Reasoning 라우팅:
#   medium (기본): 구조 파악, 로그 원인 추정, read-only 리뷰
#   high: 코드 직접 수정, 복잡 버그, 보안, 주식봇 주문/리스크/포지션, DB 마이그레이션

TASK_TYPE="${1:-read}"
REASONING="${2:-medium}"
WORKDIR="${3:-/opt/data/scripts}"
TASK="${4:-}"

export HOME=/opt/data/home
export CODEX_HOME=/opt/data/home/.codex
export PATH="$HOME/.local/bin:$HOME/.opencode/bin:$PATH"

# ─── 인수 검증 ────────────────────────────────────────────────
if [ -z "$TASK" ]; then
  echo "[codex_worker] ERROR: TASK is empty"
  echo "Usage: codex_worker.sh read|write medium|high /opt/data/scripts \"task\""
  exit 1
fi

# ─── 작업 디렉터리 제한 ────────────────────────────────────────
case "$WORKDIR" in
  /opt/data/scripts|/opt/data/scripts/*) ;;
  /opt/data/agents|/opt/data/agents/*) ;;
  /opt/data/shared|/opt/data/shared/*) ;;
  /opt/data/state|/opt/data/state/*) ;;
  *)
    echo "[codex_worker] ERROR: workdir must be under /opt/data/{scripts,agents,shared,state}"
    echo "  Provided: $WORKDIR"
    exit 3
    ;;
esac

# ─── 타입/추론 매핑 ────────────────────────────────────────────
case "$TASK_TYPE" in
  read)
    SANDBOX="read-only"
    ;;
  write)
    SANDBOX="workspace-write"
    # write 타입은 항상 high reasoning 강제
    REASONING="high"
    ;;
  *)
    echo "[codex_worker] ERROR: Unknown TASK_TYPE: $TASK_TYPE (use 'read' or 'write')"
    exit 2
    ;;
esac

# ─── reasoning 검증 ─────────────────────────────────────────────
case "$REASONING" in
  medium|high) ;;
  *)
    echo "[codex_worker] ERROR: Unknown REASONING: $REASONING (use 'medium' or 'high')"
    exit 2
    ;;
esac

# ─── Codex CLI 인증 체크 ──────────────────────────────────────
AUTH_OUTPUT=$(/opt/data/home/.local/bin/codex doctor 2>&1)
if echo "$AUTH_OUTPUT" | grep -q "✗ auth"; then
  echo "[codex_worker] ERROR: Codex CLI 인증 안 됨."
  echo "[codex_worker] 'codex login' 실행 후 재시도하세요."
  echo "[codex_worker] OpenCode Go 대신 coding_worker.sh flash/pro/heavy 레벨을 사용하세요."
  exit 10
fi

# ─── write 안전 게이트 ─────────────────────────────────────────
if [ "$TASK_TYPE" = "write" ]; then
  echo "[codex_worker] ⚠ workspace-write 모드 진입."
  echo "[codex_worker] 변경 대상: $WORKDIR"
  echo "[codex_worker] reasoning=high (코드 직접 수정, 보안, 리스크 로직)"
  echo "[codex_worker] 수정 계획 보고 후 주인님 승인 없이 실행하지 마세요."
fi

cd "$WORKDIR"

echo "[codex_worker] type=$TASK_TYPE reasoning=$REASONING sandbox=$SANDBOX workdir=$WORKDIR"
echo "[codex_worker] task: $TASK"

# ─── Codex CLI 실행 ─────────────────────────────────────────────
  /opt/data/home/.local/bin/codex exec \
    -m gpt-5.5 \
    -c "model_reasoning_effort=$REASONING" \
    -s "$SANDBOX" \
    -C "$WORKDIR" \
    --skip-git-repo-check \
    --json \
    "$TASK"

echo
echo "[codex_worker] 완료. 변경 사항 확인:"
git diff --stat 2>/dev/null || true
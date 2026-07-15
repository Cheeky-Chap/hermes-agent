#!/usr/bin/env bash
# codex_reset_status.sh — Read-only Codex 초기화권(reset credits) 조회
# Usage: ./codex_reset_status.sh
# Depends: ocx running on localhost:10100, valid Codex ChatGPT login

set -euo pipefail

OCX_PORT=10100
OCX_URL="http://127.0.0.1:${OCX_PORT}"
ACCOUNT_ID="__main__"

# 1. ocx running check
OCX_OK=$(curl -s -o /dev/null -w "%{http_code}" "${OCX_URL}/" 2>/dev/null || echo "000")
if [ "${OCX_OK}" != "200" ]; then
    echo "ocx not running (HTTP ${OCX_OK})"
    echo "Start with: ocx start --port ${OCX_PORT}"
    exit 1
fi

# 2. codex login check — verify auth.json token validity
if [ ! -f "${HOME}/.codex/auth.json" ]; then
    echo "Codex ChatGPT login required"
    echo "Run: codex login --device-auth"
    exit 1
fi
TOKEN_EXP=$(python3 -c "
import json, base64, time
with open('${HOME}/.codex/auth.json') as f:
    data = json.load(f)
t = data.get('tokens', {})
if not t.get('access_token'):
    exit(1)
parts = t['access_token'].split('.')
padded = parts[1] + '=' * (4 - len(parts[1]) % 4)
payload = json.loads(base64.urlsafe_b64decode(padded))
exp = payload.get('exp', 0)
print(int(exp > time.time()))
" 2>/dev/null || echo "0")
if [ "${TOKEN_EXP}" != "1" ]; then
    echo "Codex ChatGPT login expired or invalid"
    echo "Run: codex login --device-auth"
    echo ""
    echo "Current token expired: $(python3 -c "
import json, base64, time
from datetime import datetime
with open('${HOME}/.codex/auth.json') as f:
    data = json.load(f)
t = data.get('tokens', {})
parts = t.get('access_token','.').split('.')
if len(parts) >= 2:
    padded = parts[1] + '=' * (4 - len(parts[1]) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
    exp = payload.get('exp', 0)
    print(datetime.fromtimestamp(exp).strftime('%Y-%m-%d %H:%M:%S'))
" 2>/dev/null || echo 'unknown')"
    exit 1
fi

# 3. fetch reset credits via opencodex WHAM proxy
RESP=$(curl -s -w "\n%{http_code}" "${OCX_URL}/api/codex-auth/reset-credits?accountId=${ACCOUNT_ID}" 2>/dev/null)
HTTP_CODE=$(echo "${RESP}" | tail -1)
BODY=$(echo "${RESP}" | sed '$d')

if [ "${HTTP_CODE}" != "200" ]; then
    ERROR=$(echo "${BODY}" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('error','unknown'))" 2>/dev/null || echo "unknown")
    echo "Failed to fetch reset credits (HTTP ${HTTP_CODE})"
    echo "Reason: ${ERROR}"
    exit 1
fi

# 4. parse and display
python3 -c "
import json, sys
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))

data = json.load(sys.stdin)
count = data.get('available_count', 0)
credits = data.get('credits', [])

print(f'초기화권 남은 개수: {count}개')
print()

for i, c in enumerate(credits, 1):
    granted = datetime.fromisoformat(c['granted_at']).astimezone(KST)
    expires = datetime.fromisoformat(c['expires_at']).astimezone(KST)
    print(f'  [{i}] 지급: {granted.strftime(\"%Y-%m-%d %H:%M:%S KST\")}'
          f'  만료: {expires.strftime(\"%Y-%m-%d %H:%M:%S KST\")}'
          f'  ({(expires - datetime.now(KST)).days}일 남음)')
" <<< "${BODY}"

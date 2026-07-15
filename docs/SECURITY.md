# Security policy

## 저장소에 포함하지 않는 정보

- 실제 `.env`와 모든 API key, token, password, webhook URL
- SSH private key와 Codex/OpenCode 인증 파일
- `state.db`, `*.db`, SQLite 파일과 거래 데이터
- `positions.json`, 계좌 상태와 주문·체결 데이터
- 로그, 세션, 캐시, 메모리, runtime lock/PID
- 백업·압축 파일, 모델·대용량 데이터

## 런타임 원칙

- master의 허용 사용자만 승인 명령을 보낼 수 있습니다.
- work 채널은 outbound 감사 전용입니다.
- replay 방지를 위해 Discord message/channel/user identity를 묶습니다.
- Codex는 허용된 단일 workspace의 sanitized copy만 봅니다.
- 삭제, rename, symlink, binary, scope 밖 변경은 거부합니다.
- 비밀값과 유사한 입력·출력·diff는 차단합니다.
- Alpaca 주문·취소·청산 동작은 Hermes/Codex 경계 밖입니다.

## 공개 전 검사

내보내기에는 다음 검사를 모두 적용합니다.

1. 운영 비밀값과의 exact byte 비교
2. private key, JWT, Discord webhook, OpenAI/GitHub token 패턴 검사
3. Git ignore 대상과 파일 확장자 검사
4. Git index에 포함될 파일만 다시 검사

테스트용 placeholder는 실제 자격정보가 아니어야 하며 실제 값과 exact
match가 하나라도 발견되면 push하지 않습니다.

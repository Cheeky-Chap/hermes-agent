# Export policy

## 포함

- 현재 `services/hermes-codex-broker`의 Python 소스와 shell 실행기
- Hermes Discord plugin과 plugin manifest
- 오프라인 단위·보안 회귀 테스트
- 선택된 Hermes 실행 정책 `AGENTS.md`와 persona `SOUL.md`
- 읽기 전용 Codex reset-credit 상태 도구
- placeholder만 포함한 환경변수·Hermes 설정 예시
- 설치·운영·보안 문서

## 제외

- `/opt/data/.env`와 모든 사본
- `state/hermes-master/home`의 DB, auth, sessions, logs, cache, memories
- 공식 이미지가 runtime에 설치한 bundled skills와 바이너리
- `/opt/data/legacy/hermes` 전체와 오래된 bridge/Serena 백업
- 주식봇, TFT, Misaka, daily-report 소스와 상태
- `state/`, `logs/`, `backup/`, `backups/`
- 거래·계좌 데이터, 모델, 데이터셋, 압축 파일

운영 원본은 복사만 하며 이동·수정·삭제하지 않습니다. 이 저장소의 예시
설정은 실제 운영 자격정보를 복원하는 용도로 사용할 수 없습니다.

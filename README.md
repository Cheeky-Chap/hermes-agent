# hermes-agent

Discord에서 동작하는 공식 Hermes Agent 게이트웨이와, 사람이 승인한 작업만
격리된 Codex CLI로 전달하는 호스트 브로커입니다. Hermes는 일반 대화를
DeepSeek로 처리하며 파일·터미널·Docker·거래 도구를 직접 갖지 않습니다.

## 구조

```text
.
├── AGENTS.md                 # Hermes의 실행 경계
├── agents/hermes/SOUL.md     # 대화 성격과 응답 원칙
├── hermes_plugin/            # Discord ↔ 호스트 브로커 플러그인
├── tests/                    # 오프라인 단위·보안 회귀 테스트
├── scripts/                  # 선택적 읽기 전용 운영 도구
├── docs/                     # 아키텍처·보안·내보내기 정책
├── broker.py                 # 승인 큐와 안전한 반영 트랜잭션
├── codex_runner.py           # Codex/bubblewrap 격리 실행기
├── launch_container.py       # 고정 이미지 기반 Docker 실행기
├── discord_audit_check.py    # Discord 구성 점검 도구
├── config.example.yaml
└── .env.example
```

## 주요 구성 요소

- 공식 Hermes Agent 0.18.2 Discord gateway
- DeepSeek 기반 일반 대화
- master 채널의 `승인 JOB-ID`, `수정 승인 JOB-ID`, `상태 JOB-ID`,
  `취소 JOB-ID` 명령
- work 채널의 읽기 전용 감사 기록과 Codex 결과
- 한 번에 하나의 Codex 작업, 최대 5개 대기열, 300초 제한
- 비밀 파일과 symlink를 제외한 staging 분석
- 승인 범위 SHA-256, live drift 검사, atomic replace, rollback journal

## 요구 사항

- Linux
- Python 3.12 이상
- Docker Engine
- tmux
- bubblewrap(`bwrap`)
- Codex CLI 0.144.3
- 공식 Hermes 이미지
  `nousresearch/hermes-agent@sha256:3db34ce19adfa080736a2a3feb0316dbcccc588faa9afe7fd8ae1c03b4f1a53a`

Python 테스트 의존성:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

## 환경변수

```bash
cp .env.example .env
chmod 600 .env
```

필수 값은 다음과 같습니다.

- `HERMES_DISCORD_BOT_TOKEN`: Hermes Discord bot token
- `DEEPSEEK_API_KEY`: DeepSeek 공식 API key
- `DISCORD_ALLOWED_USERS`: 승인 가능한 Discord 사용자 ID 목록

`.env`에는 실제 값을 입력하되 절대 커밋하지 않습니다. Discord 채널 ID와
웹훅 이름은 배포 환경에 맞게 예시 설정과 플러그인 환경변수를 조정합니다.

## 설정

```bash
cp config.example.yaml /path/to/hermes-home/config.yaml
```

`config.example.yaml`은 master 입력과 work 출력만 허용하고, Hermes의 직접
파일·터미널·웹·거래 도구를 비활성화하는 기준 예시입니다.

## 실행과 점검

오프라인 검사:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s tests -v
bash -n start.sh run-background.sh ensure-running.sh status.sh stop.sh
```

호스트 브로커:

```bash
./run-background.sh
./status.sh
```

Docker 실행기는 실제 Docker 상태와 전용 홈, 플러그인, IPC 경로가 준비된
운영 환경에서만 사용합니다.

```bash
python3 launch_container.py --check
python3 launch_container.py --create
```

`--create`는 컨테이너를 생성하므로 예시 경로와 환경변수를 먼저 검토해야
합니다. Docker Compose는 사용하지 않습니다. 비밀값을 명령행 인자로
노출하지 않고 정확한 mount/capability를 검사하기 위해 전용 실행기를
사용합니다.

## 현재 미니PC 운영 구조

현재 배포에서는 다음 경계를 사용합니다.

- 소스: `/opt/data/services/hermes-codex-broker`
- Hermes 전용 홈: `/opt/data/state/hermes-master/home`
- 브로커 상태: `/opt/data/state/hermes-codex-broker`
- 브로커 로그: `/opt/data/logs/hermes-codex-broker`
- 컨테이너: `hermes-master-v0182`
- master: 소유자 대화와 승인 명령
- work: outbound 감사 기록

주식봇, TFT, Misaka, 일일보고는 별도 프로젝트이며 이 저장소에 포함되지
않습니다. 브로커는 배포 시 명시적으로 허용된 workspace만 staging으로
복사합니다.

## 보안과 제외 항목

이 저장소에는 실제 `.env`, API key, Discord token, webhook URL, Alpaca
정보, SSH key, Codex/OpenCode 인증, DB, 로그, 세션, 캐시, 메모리, 거래
상태, 백업, 모델 파일이 포함되지 않습니다. 예시 설정에는 placeholder만
있습니다. 자세한 기준은 [보안 문서](docs/SECURITY.md)와
[내보내기 정책](docs/EXPORT_POLICY.md)을 참고하십시오.

## 알려진 제한

현재 보관된 구현의 `auth_wrapper.py`는 Codex 인증을 일회성 FIFO로
전달합니다. Codex CLI 0.144.3의 실제 요청에서 인증 파일을 다시 읽을 때
`401 Missing bearer authentication`이 발생할 수 있음이 확인됐습니다.
이 저장소는 현재 운영 소스를 리팩터링하지 않고 안전하게 보관한
스냅샷이며, 인증 프록시 전환은 별도 변경으로 검토해야 합니다.

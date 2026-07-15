# Architecture

```text
Discord master
      |
      v
Official Hermes Agent container -- DeepSeek
      |
      v
hermes-codex-bridge plugin
      |
      v
read-only Unix socket
      |
      v
host Codex broker
      |
      +-- sanitized analysis copy (read-only)
      |
      +-- scope-hashed staging copy (second approval)
      |
      v
Discord work audit
```

Hermes는 직접 파일이나 호스트 명령을 실행하지 않습니다. 프로젝트 변경
요청은 먼저 제안 상태로 기록되며, 허용된 사용자가 master 채널에서 승인한
경우에만 호스트 브로커가 Codex를 실행합니다.

분석 단계는 비밀·상태·로그·백업·symlink를 제외한 복사본을 사용합니다.
반영 단계는 별도 staging에서 실행되고, 분석 당시 fingerprint와 승인
scope hash가 모두 일치할 때만 regular text file을 atomic replace합니다.

컨테이너에는 Docker socket이나 전체 호스트 `/opt/data`가 주어지지
않습니다. Hermes 전용 홈, 읽기 전용 플러그인, 읽기 전용 IPC만 mount하는
것이 기본 경계입니다.

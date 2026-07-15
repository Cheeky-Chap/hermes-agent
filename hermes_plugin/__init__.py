from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Coroutine


MASTER_CHANNEL_ID = os.environ.get("HERMES_MASTER_CHANNEL_ID", "1526744995623862272").strip()
WORK_CHANNEL_ID = os.environ.get("HERMES_WORK_CHANNEL_ID", "1526751882092220426").strip()
BROKER_SOCKET = os.environ.get("HERMES_CODEX_BROKER_SOCKET", "/run/hermes-codex/broker.sock").strip()
WEBHOOK_NAME = os.environ.get("HERMES_CODEX_WEBHOOK_NAME", "Captain Hook").strip()
DISCORD_API = "https://discord.com/api/v10"

ALLOWED_TOOLS = frozenset({"memory", "clarify", "codex_propose"})
WORKSPACES = ("stocks", "tft", "misaka", "daily-report")
JOB_ID_PATTERN = r"HC-[0-9]{8}-[A-F0-9]{4}"
COMMAND_RE = re.compile(
    rf"^(?P<command>수정 승인|승인|상태|취소) (?P<job>{JOB_ID_PATTERN})$",
)
DISCORD_SNOWFLAKE_RE = re.compile(r"^[0-9]{15,22}$")
SCOPE_HASH_RE = re.compile(r"^[A-F0-9]{64}$")
MAX_BROKER_REQUEST_BYTES = 128 * 1024
MAX_BROKER_RESPONSE_BYTES = 512 * 1024
BROKER_TIMEOUT_SECONDS = 5.0

_runtime_lock = threading.Lock()
_runtime_loop: asyncio.AbstractEventLoop | None = None
_runtime_gateway: Any = None
_published: set[tuple[str, str, int]] = set()
_watching: set[tuple[str, str]] = set()
_background_tasks: set[asyncio.Task[Any]] = set()


@dataclass(frozen=True)
class DiscordOrigin:
    """Immutable Discord identity attached to every user control request."""

    message_id: str
    channel_id: str
    user_id: str

    def broker_fields(self, *, require_message_id: bool = True) -> dict[str, str]:
        if not DISCORD_SNOWFLAKE_RE.fullmatch(self.channel_id):
            return {}
        if not DISCORD_SNOWFLAKE_RE.fullmatch(self.user_id):
            return {}
        if require_message_id and not DISCORD_SNOWFLAKE_RE.fullmatch(self.message_id):
            return {}
        fields = {
            "channel_id": self.channel_id,
            "user_id": self.user_id,
        }
        if DISCORD_SNOWFLAKE_RE.fullmatch(self.message_id):
            fields["message_id"] = self.message_id
        return fields


def _control_payload(
    operation: str,
    origin: DiscordOrigin,
    **fields: Any,
) -> dict[str, Any] | None:
    identity = origin.broker_fields(require_message_id=True)
    if not identity:
        return None
    return {"op": operation, **fields, **identity}


def _allowed_users() -> set[str]:
    users: set[str] = set()
    entries = [entry for entry in os.environ.get("DISCORD_ALLOWED_USERS", "").split(",") if entry.strip()]
    if not entries:
        return set()
    for raw in entries:
        cleaned = raw.strip().removeprefix("user:").strip()
        cleaned = cleaned.replace("<@!", "").replace("<@", "").replace(">", "")
        if not (cleaned.isdigit() and 15 <= len(cleaned) <= 22):
            return set()
        users.add(cleaned)
    return users


def _platform_name(source: Any) -> str:
    platform = getattr(source, "platform", "")
    return str(getattr(platform, "value", platform) or "").lower()


def _safe_chat_text(value: Any, limit: int = 6_000) -> str:
    text = str(value or "").strip()[:limit]
    return text.replace("@", "@\u200b")


def _remember_runtime(gateway: Any) -> None:
    global _runtime_loop, _runtime_gateway
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    with _runtime_lock:
        _runtime_loop = loop
        _runtime_gateway = gateway


def _schedule(coro: Coroutine[Any, Any, Any]) -> bool:
    with _runtime_lock:
        loop = _runtime_loop
    if loop is None or loop.is_closed():
        coro.close()
        return False

    def create_task() -> None:
        task = loop.create_task(coro)
        _background_tasks.add(task)

        def finished(done: asyncio.Task[Any]) -> None:
            _background_tasks.discard(done)
            try:
                done.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(finished)

    try:
        loop.call_soon_threadsafe(create_task)
        return True
    except RuntimeError:
        coro.close()
        return False


def _broker_request(payload: dict[str, Any], timeout: float = BROKER_TIMEOUT_SECONDS) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"ok": False, "error": {"code": "invalid_request", "message": "invalid broker request"}}
    try:
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    except (TypeError, ValueError):
        return {"ok": False, "error": {"code": "invalid_request", "message": "invalid broker request"}}
    if len(encoded) > MAX_BROKER_REQUEST_BYTES:
        return {"ok": False, "error": {"code": "request_too_large", "message": "broker request too large"}}
    if not (BROKER_SOCKET.startswith("/") and len(BROKER_SOCKET) <= 255):
        return {"ok": False, "error": {"code": "broker_unavailable", "message": "Codex broker is unavailable"}}
    try:
        effective_timeout = min(max(float(timeout), 0.1), 10.0)
    except (TypeError, ValueError):
        effective_timeout = BROKER_TIMEOUT_SECONDS
    client: socket.socket | None = None
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(effective_timeout)
        client.connect(BROKER_SOCKET)
        client.sendall(encoded)
        buffer = bytearray()
        while b"\n" not in buffer:
            chunk = client.recv(64 * 1024)
            if not chunk:
                break
            buffer.extend(chunk)
            if len(buffer) > MAX_BROKER_RESPONSE_BYTES:
                raise ValueError("broker response too large")
        if b"\n" not in buffer:
            raise ValueError("unterminated broker response")
        line = bytes(buffer).split(b"\n", 1)[0]
        response = json.loads(line.decode("utf-8"))
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            return {"ok": False, "error": {"code": "bad_response", "message": "invalid broker response"}}
        return response
    except (OSError, ValueError, json.JSONDecodeError):
        return {"ok": False, "error": {"code": "broker_unavailable", "message": "Codex broker is unavailable"}}
    finally:
        if client is not None:
            client.close()


async def _send_bot(channel_id: str, content: str) -> bool:
    with _runtime_lock:
        gateway = _runtime_gateway
    if gateway is None:
        return False
    try:
        from gateway.config import Platform

        adapter = gateway.adapters.get(Platform.DISCORD)
        if adapter is None:
            return False
        result = await adapter.send(
            str(channel_id),
            _safe_chat_text(content, 20_000),
            metadata={"non_conversational": True, "notify": True},
        )
        return bool(getattr(result, "success", False))
    except Exception:
        return False


def _discover_webhook_sync() -> tuple[str, str] | None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        return None
    request = urllib.request.Request(
        f"{DISCORD_API}/channels/{WORK_CHANNEL_ID}/webhooks",
        headers={"Authorization": f"Bot {token}", "User-Agent": "HermesCodexBridge/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read(256 * 1024).decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.HTTPError):
        return None
    if not isinstance(payload, list):
        return None
    matches: list[tuple[str, str]] = []
    for entry in payload:
        if (
            not isinstance(entry, dict)
            or entry.get("name") != WEBHOOK_NAME
            or str(entry.get("channel_id") or "") != WORK_CHANNEL_ID
            or entry.get("type") not in (1, "1")
        ):
            continue
        webhook_id = str(entry.get("id") or "")
        webhook_token = str(entry.get("token") or "")
        if webhook_id.isdigit() and webhook_token:
            matches.append((webhook_id, webhook_token))
    if len(matches) != 1:
        return None
    return matches[0]


def _post_webhook_chunk_sync(content: str) -> bool:
    webhook = _discover_webhook_sync()
    if webhook is None:
        return False
    webhook_id, webhook_token = webhook
    query = urllib.parse.urlencode({"wait": "true"})
    url = f"{DISCORD_API}/webhooks/{webhook_id}/{webhook_token}?{query}"
    payload = json.dumps(
        {
            "content": _safe_chat_text(content, 1_850),
            "username": "Codex",
            "allowed_mentions": {"parse": []},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "HermesCodexBridge/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.HTTPError):
        return False


def _chunks(text: str, limit: int = 1_700) -> list[str]:
    remaining = _safe_chat_text(text, 50_000)
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks or ["(empty Codex result)"]


async def _send_codex(job_id: str, label: str, text: str) -> bool:
    pieces = _chunks(text)
    all_sent = True
    for index, piece in enumerate(pieces, start=1):
        header = f"[{job_id}] Codex {label}"
        if len(pieces) > 1:
            header += f" ({index}/{len(pieces)})"
        sent = await asyncio.to_thread(_post_webhook_chunk_sync, f"**{header}**\n{piece}")
        all_sent = all_sent and sent
    if not all_sent:
        await _send_bot(WORK_CHANNEL_ID, f"[{job_id}] Codex webhook delivery failed. Result is retained in the broker state; use `상태 {job_id}` in master.")
    return all_sent


def _error_text(response: dict[str, Any]) -> str:
    error = response.get("error") if isinstance(response, dict) else None
    if not isinstance(error, dict):
        return "알 수 없는 브로커 오류"
    code = _safe_chat_text(error.get("code"), 100)
    message = _safe_chat_text(error.get("message"), 500)
    return f"{code}: {message}" if code else message


async def _publish_terminal(job: dict[str, Any]) -> None:
    job_id = str(job.get("id") or "")
    status = str(job.get("status") or "")
    version = int(job.get("version") or 0)
    marker = (job_id, status, version)
    if marker in _published:
        return

    if status == "analysis_ready":
        analysis = str(job.get("analysis_result") or "")
        if not await _send_codex(job_id, "읽기 전용 분석", analysis):
            return
        _published.add(marker)
        scope_hash = _safe_chat_text(job.get("scope_hash"), 100)
        paths = ", ".join(_safe_chat_text(path, 300) for path in (job.get("scope_paths") or []))
        await _send_bot(
            WORK_CHANNEL_ID,
            f"[{job_id}] Hermes 검토: 읽기 전용 분석이 끝났어. 범위 해시 `{scope_hash}`, 대상: {paths or '없음'}. 실제 반영은 아직 없었어.",
        )
        await _send_bot(
            MASTER_CHANNEL_ID,
            f"[{job_id}] Codex 읽기 전용 분석 완료. 범위 해시 `{scope_hash}`. work 채널에서 분석을 확인한 뒤 변경하려면 `수정 승인 {job_id}`, 중단하려면 `취소 {job_id}`라고 말해줘.",
        )
    elif status == "applied":
        result = str(job.get("apply_result") or "")
        if not await _send_codex(job_id, "승인된 구현 결과", result):
            return
        _published.add(marker)
        changed = ", ".join(_safe_chat_text(path, 300) for path in (job.get("changed_paths") or [])) or "변경 없음"
        await _send_bot(WORK_CHANNEL_ID, f"[{job_id}] Hermes 검토: 승인 범위 반영 완료. 변경 파일: {changed}")
        await _send_bot(MASTER_CHANNEL_ID, f"[{job_id}] 승인 범위 반영 완료. 변경 파일: {changed}. 자세한 Codex 결과는 work 채널에 있어.")
    elif status in {"failed", "security_hold", "stale_analysis", "cancelled"}:
        _published.add(marker)
        code = _safe_chat_text(job.get("error_code"), 100)
        message = _safe_chat_text(job.get("error_message"), 600)
        await _send_bot(WORK_CHANNEL_ID, f"[{job_id}] Hermes 상태: `{status}` ({code}) {message}")
        await _send_bot(MASTER_CHANNEL_ID, f"[{job_id}] `{status}` ({code}) {message}")


async def _watch_job(job_id: str, phase: str, origin: DiscordOrigin) -> None:
    watch_key = (job_id, phase)
    if watch_key in _watching:
        return
    _watching.add(watch_key)
    deadline = time.monotonic() + 1_900
    try:
        while time.monotonic() < deadline:
            payload = _control_payload("get", origin, job_id=job_id)
            if payload is None:
                await _send_bot(MASTER_CHANNEL_ID, f"[{job_id}] 원본 Discord 메시지 검증에 실패해서 상태 감시를 중단했어.")
                return
            response = await asyncio.to_thread(_broker_request, payload)
            if not response.get("ok"):
                await _send_bot(MASTER_CHANNEL_ID, f"[{job_id}] 상태 조회 실패: {_error_text(response)}")
                return
            job = response.get("result") or {}
            status = str(job.get("status") or "")
            if status in {"analysis_ready", "applied", "failed", "security_hold", "stale_analysis", "cancelled"}:
                await _publish_terminal(job)
                return
            await asyncio.sleep(3)
        await _send_bot(MASTER_CHANNEL_ID, f"[{job_id}] 감시 제한 시간이 지났어. 작업은 취소하지 않았고 `상태 {job_id}`로 확인할 수 있어.")
    finally:
        _watching.discard(watch_key)


async def _notify_proposal(job: dict[str, Any], origin: DiscordOrigin) -> None:
    job_id = _safe_chat_text(job.get("id"), 100)
    workspace = _safe_chat_text(job.get("workspace"), 100)
    objective = _safe_chat_text(job.get("objective"), 3_000)
    sent = await _send_bot(
        WORK_CHANNEL_ID,
        f"[{job_id}] Hermes → Codex 제안\nWorkspace: `{workspace}`\n요청: {objective}\n아직 Codex 실행이나 파일 변경은 없으며 master의 `승인 {job_id}`가 필요해.",
    )
    if not sent:
        identity = origin.broker_fields(require_message_id=False)
        await asyncio.to_thread(
            _broker_request,
            {"op": "abandon", "job_id": job_id, **identity},
        )
        await _send_bot(MASTER_CHANNEL_ID, f"[{job_id}] work 감사 기록 전달에 실패해서 이 제안을 폐기했어. 새로 요청해줘.")


async def _handle_command(command: str, job_id: str, origin: DiscordOrigin) -> None:
    operation = {
        "승인": "approve_analysis",
        "수정 승인": "approve_apply",
        "상태": "get",
        "취소": "cancel",
    }.get(command)
    if operation is None:
        await _send_bot(MASTER_CHANNEL_ID, "지원하지 않는 승인 명령이야.")
        return

    payload = _control_payload(operation, origin, job_id=job_id)
    if payload is None:
        await _send_bot(MASTER_CHANNEL_ID, f"[{job_id}] 원본 Discord 메시지 식별자를 검증할 수 없어 요청을 거부했어.")
        return

    if operation == "approve_apply":
        lookup = _control_payload("get", origin, job_id=job_id)
        if lookup is None:
            await _send_bot(MASTER_CHANNEL_ID, f"[{job_id}] 승인 범위를 다시 조회할 수 없어 요청을 거부했어.")
            return
        lookup_response = await asyncio.to_thread(_broker_request, lookup)
        if not lookup_response.get("ok"):
            await _send_bot(MASTER_CHANNEL_ID, f"[{job_id}] 승인 범위 조회 거부: {_error_text(lookup_response)}")
            return
        lookup_job = lookup_response.get("result") or {}
        if not isinstance(lookup_job, dict) or str(lookup_job.get("id") or "") != job_id:
            await _send_bot(MASTER_CHANNEL_ID, f"[{job_id}] 브로커가 다른 작업 정보를 반환해서 수정 승인을 중단했어.")
            return
        if str(lookup_job.get("status") or "") != "analysis_ready":
            await _send_bot(MASTER_CHANNEL_ID, f"[{job_id}] 읽기 전용 분석이 완료된 상태가 아니어서 수정 승인을 거부했어.")
            return
        scope_hash = str(lookup_job.get("scope_hash") or "")
        if not SCOPE_HASH_RE.fullmatch(scope_hash):
            await _send_bot(MASTER_CHANNEL_ID, f"[{job_id}] 64자리 승인 범위 해시가 없어 수정 승인을 거부했어. 먼저 읽기 전용 분석을 다시 실행해줘.")
            return
        payload["scope_hash"] = scope_hash

    response = await asyncio.to_thread(_broker_request, payload)
    if not response.get("ok"):
        await _send_bot(MASTER_CHANNEL_ID, f"[{job_id}] 요청 거부: {_error_text(response)}")
        return
    job = response.get("result") or {}
    status = _safe_chat_text(job.get("status"), 100)
    if operation == "get":
        scope_hash = _safe_chat_text(job.get("scope_hash"), 100)
        changed = ", ".join(_safe_chat_text(path, 200) for path in (job.get("changed_paths") or []))
        suffix = f", scope `{scope_hash}`" if scope_hash else ""
        if changed:
            suffix += f", changed: {changed}"
        await _send_bot(MASTER_CHANNEL_ID, f"[{job_id}] 상태 `{status}`{suffix}")
        if status in {"analysis_ready", "applied"}:
            await _publish_terminal(job)
    elif operation == "cancel":
        await _send_bot(MASTER_CHANNEL_ID, f"[{job_id}] 취소 요청 처리: `{status}`")
        if status == "cancel_requested":
            _schedule(_watch_job(job_id, "cancel", origin))
    elif operation == "approve_analysis":
        await _send_bot(MASTER_CHANNEL_ID, f"[{job_id}] 읽기 전용 Codex 분석을 큐에 넣었어. 파일 변경은 허용되지 않아.")
        await _send_bot(WORK_CHANNEL_ID, f"[{job_id}] Hermes: 사용자가 읽기 전용 분석을 승인했어. Codex 응답을 기다리는 중이야.")
        _schedule(_watch_job(job_id, "analysis", origin))
    elif operation == "approve_apply":
        await _send_bot(MASTER_CHANNEL_ID, f"[{job_id}] 해시로 고정된 분석 범위의 staging 구현을 승인했어. 범위 검증을 통과한 파일만 반영돼.")
        await _send_bot(WORK_CHANNEL_ID, f"[{job_id}] Hermes: 사용자가 scope `{_safe_chat_text(job.get('scope_hash'), 100)}` 구현을 승인했어.")
        _schedule(_watch_job(job_id, "apply", origin))


def _on_pre_gateway_dispatch(event: Any = None, gateway: Any = None, **_: Any) -> dict[str, str]:
    if event is None or gateway is None:
        return {"action": "skip", "reason": "missing bridge event context"}
    _remember_runtime(gateway)
    source = getattr(event, "source", None)
    if source is None or _platform_name(source) != "discord":
        return {"action": "skip", "reason": "Hermes bridge accepts Discord master only"}
    chat_id = str(getattr(source, "chat_id", "") or "")
    if chat_id != MASTER_CHANNEL_ID:
        return {"action": "skip", "reason": "non-master channel is audit-only or out of scope"}
    if bool(getattr(source, "is_bot", False)):
        return {"action": "skip", "reason": "bot and webhook messages are never commands"}
    user_id = str(getattr(source, "user_id", "") or "")
    if not user_id or user_id not in _allowed_users():
        return {"action": "skip", "reason": "sender is not independently authorized"}

    text = str(getattr(event, "text", "") or "")
    if text.startswith("/"):
        _schedule(
            _send_bot(
                MASTER_CHANNEL_ID,
                "이 Hermes 인스턴스에서는 slash/관리 명령을 사용할 수 없어. 일반 질문이나 승인 명령만 처리해.",
            )
        )
        return {"action": "skip", "reason": "slash and gateway management commands are disabled"}
    match = COMMAND_RE.fullmatch(text)
    if match:
        message_id = str(
            getattr(event, "message_id", "")
            or getattr(source, "message_id", "")
            or ""
        )
        origin = DiscordOrigin(
            message_id=message_id,
            channel_id=chat_id,
            user_id=user_id,
        )
        if not origin.broker_fields(require_message_id=True):
            _schedule(
                _send_bot(
                    MASTER_CHANNEL_ID,
                    "원본 Discord 메시지 ID를 검증할 수 없어 승인 명령을 처리하지 않았어. 새 메시지로 다시 시도해줘.",
                )
            )
            return {"action": "skip", "reason": "approval command lacked replay-safe Discord identity"}
        _schedule(_handle_command(match.group("command"), match.group("job").upper(), origin))
        return {"action": "skip", "reason": "bridge approval command handled without LLM"}
    if re.match(r"^(?:수정\s+승인|승인|상태|취소)\b", text.strip()):
        _schedule(
            _send_bot(
                MASTER_CHANNEL_ID,
                "명령 형식은 `승인 HC-YYYYMMDD-ABCD`, `수정 승인 HC-YYYYMMDD-ABCD`, `상태 HC-YYYYMMDD-ABCD`, `취소 HC-YYYYMMDD-ABCD`야.",
            )
        )
        return {"action": "skip", "reason": "malformed approval command rejected"}
    return {"action": "allow"}


def _on_pre_tool_call(tool_name: str = "", **_: Any) -> dict[str, str] | None:
    if tool_name in ALLOWED_TOOLS:
        return None
    return {
        "action": "block",
        "message": (
            f"Tool '{tool_name}' is blocked by the local answer-only policy. "
            "Hermes may only use memory, clarify, or create a human-approved Codex proposal."
        ),
    }


def _handle_codex_propose(args: dict[str, Any], **_: Any) -> str:
    try:
        from gateway.session_context import get_session_env

        platform = get_session_env("HERMES_SESSION_PLATFORM", "")
        chat_id = get_session_env("HERMES_SESSION_CHAT_ID", "")
        user_id = get_session_env("HERMES_SESSION_USER_ID", "")
        message_id = get_session_env("HERMES_SESSION_MESSAGE_ID", "")
    except Exception:
        return json.dumps({"ok": False, "error": "missing trusted gateway session context"}, ensure_ascii=False)
    if platform != "discord" or chat_id != MASTER_CHANNEL_ID or user_id not in _allowed_users():
        return json.dumps({"ok": False, "error": "proposal rejected outside the authorized master session"}, ensure_ascii=False)
    if not isinstance(args, dict) or set(args) - {"objective", "workspace"}:
        return json.dumps({"ok": False, "error": "proposal arguments were rejected"}, ensure_ascii=False)
    objective = str(args.get("objective") or "").strip()
    workspace = str(args.get("workspace") or "").strip()
    if workspace not in WORKSPACES:
        return json.dumps({"ok": False, "error": "unsupported workspace"}, ensure_ascii=False)
    if not objective or len(objective) > 8_000:
        return json.dumps({"ok": False, "error": "objective must contain 1-8000 characters"}, ensure_ascii=False)
    origin = DiscordOrigin(
        message_id=str(message_id or ""),
        channel_id=str(chat_id),
        user_id=str(user_id),
    )
    identity = origin.broker_fields(require_message_id=False)
    if not identity:
        return json.dumps({"ok": False, "error": "proposal origin was rejected"}, ensure_ascii=False)
    if message_id and not DISCORD_SNOWFLAKE_RE.fullmatch(str(message_id)):
        return json.dumps({"ok": False, "error": "proposal message identity was rejected"}, ensure_ascii=False)
    response = _broker_request(
        {
            "op": "submit",
            "objective": objective,
            "workspace": workspace,
            **identity,
        }
    )
    if not response.get("ok"):
        return json.dumps({"ok": False, "error": _error_text(response)}, ensure_ascii=False)
    job = response.get("result") or {}
    if not isinstance(job, dict):
        return json.dumps({"ok": False, "error": "broker returned an invalid job"}, ensure_ascii=False)
    job_id = str(job.get("id") or "")
    if not re.fullmatch(JOB_ID_PATTERN, job_id):
        return json.dumps({"ok": False, "error": "broker returned an invalid job identity"}, ensure_ascii=False)
    _schedule(_notify_proposal(job, origin))
    return json.dumps(
        {
            "ok": True,
            "job_id": job.get("id"),
            "status": job.get("status"),
            "message": (
                f"Proposal created. No Codex run or file change has occurred. "
                f"Tell the user to review work and type `승인 {job.get('id')}` in master for read-only analysis."
            ),
        },
        ensure_ascii=False,
    )


CODEX_PROPOSE_SCHEMA = {
    "name": "codex_propose",
    "description": (
        "Create a pending Codex job proposal when the user explicitly asks for code, configuration, or project changes. "
        "This tool does not run Codex and cannot modify files. A separate exact human approval command is required. "
        "Do not use it for questions that Hermes can answer directly."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "objective": {
                "type": "string",
                "maxLength": 8000,
                "description": "Concrete requested outcome without secrets or invented permissions.",
            },
            "workspace": {
                "type": "string",
                "enum": list(WORKSPACES),
                "description": "The single isolated project workspace Codex may inspect.",
            },
        },
        "required": ["objective", "workspace"],
        "additionalProperties": False,
    },
}


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="codex_propose",
        toolset="hermes_codex_bridge",
        schema=CODEX_PROPOSE_SCHEMA,
        handler=_handle_codex_propose,
        description="Create a human-approved, isolated Codex proposal.",
        emoji="🧭",
    )
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)

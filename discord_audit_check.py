#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from launch_container import LaunchError, load_selected_env


DISCORD_API = "https://discord.com/api/v10"
MASTER_CHANNEL_ID = "1526744995623862272"
WORK_CHANNEL_ID = "1526751882092220426"
WEBHOOK_NAME = "Captain Hook"


class AuditCheckError(RuntimeError):
    pass


def _json_request(
    url: str,
    *,
    token: str | None = None,
    payload: Any | None = None,
    method: str | None = None,
) -> Any:
    headers = {"User-Agent": "HermesCodexDeploymentCheck/1.0"}
    if token is not None:
        headers["Authorization"] = f"Bot {token}"
    data = None
    request_method = method or "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        request_method = method or "POST"
    request = urllib.request.Request(url, data=data, headers=headers, method=request_method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = response.read(512 * 1024)
            if not 200 <= int(response.status) < 300:
                raise AuditCheckError("Discord returned an unsuccessful status")
    except (OSError, urllib.error.HTTPError) as exc:
        raise AuditCheckError("Discord connectivity check failed") from exc
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditCheckError("Discord returned an invalid response") from exc


def _discover(token: str) -> tuple[str, str, str, str, int, int]:
    bot = _json_request(f"{DISCORD_API}/users/@me", token=token)
    master = _json_request(f"{DISCORD_API}/channels/{MASTER_CHANNEL_ID}", token=token)
    work = _json_request(f"{DISCORD_API}/channels/{WORK_CHANNEL_ID}", token=token)
    hooks = _json_request(f"{DISCORD_API}/channels/{WORK_CHANNEL_ID}/webhooks", token=token)
    if not all(isinstance(item, dict) for item in (bot, master, work)) or not isinstance(hooks, list):
        raise AuditCheckError("Discord objects could not be verified")
    matches = [
        item
        for item in hooks
        if isinstance(item, dict)
        and item.get("name") == WEBHOOK_NAME
        and str(item.get("channel_id") or "") == WORK_CHANNEL_ID
        and item.get("type") in (1, "1")
        and str(item.get("id") or "").isdigit()
        and bool(item.get("token"))
    ]
    if len(matches) != 1:
        raise AuditCheckError("exactly one expected work webhook was not found")
    application_id = str(bot.get("id") or "")
    guild_id = str(work.get("guild_id") or "")
    if not application_id.isdigit() or not guild_id.isdigit():
        raise AuditCheckError("Discord application or guild identity was invalid")
    global_commands = _json_request(f"{DISCORD_API}/applications/{application_id}/commands", token=token)
    guild_commands = _json_request(
        f"{DISCORD_API}/applications/{application_id}/guilds/{guild_id}/commands",
        token=token,
    )
    if not isinstance(global_commands, list) or not isinstance(guild_commands, list):
        raise AuditCheckError("Discord slash command state was invalid")
    return (
        str(matches[0]["id"]),
        str(matches[0]["token"]),
        application_id,
        guild_id,
        len(global_commands),
        len(guild_commands),
    )


def _clear_commands(token: str, application_id: str, guild_id: str) -> None:
    global_result = _json_request(
        f"{DISCORD_API}/applications/{application_id}/commands",
        token=token,
        payload=[],
        method="PUT",
    )
    guild_result = _json_request(
        f"{DISCORD_API}/applications/{application_id}/guilds/{guild_id}/commands",
        token=token,
        payload=[],
        method="PUT",
    )
    if global_result != [] or guild_result != []:
        raise AuditCheckError("Discord slash command removal could not be verified")


def _bot_post(token: str, channel_id: str, content: str) -> None:
    _json_request(
        f"{DISCORD_API}/channels/{channel_id}/messages",
        token=token,
        payload={"content": content, "allowed_mentions": {"parse": []}},
    )


def _webhook_post(webhook_id: str, webhook_token: str, content: str) -> None:
    query = urllib.parse.urlencode({"wait": "true"})
    _json_request(
        f"{DISCORD_API}/webhooks/{webhook_id}/{webhook_token}?{query}",
        payload={"content": content, "username": "Codex", "allowed_mentions": {"parse": []}},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Secret-safe Hermes Discord deployment check")
    parser.add_argument("--send-audit", action="store_true", help="post bounded deployment audit messages")
    parser.add_argument(
        "--clear-slash-commands",
        action="store_true",
        help="remove stale global and guild slash commands for this bot",
    )
    args = parser.parse_args()
    try:
        token = load_selected_env()["HERMES_DISCORD_BOT_TOKEN"]
        webhook_id, webhook_token, application_id, guild_id, global_count, guild_count = _discover(token)
        print("discord_bot=ok")
        print("master_channel=ok")
        print("work_channel=ok")
        print("captain_hook=exactly_one")
        print(f"global_slash_commands={global_count}")
        print(f"guild_slash_commands={guild_count}")
        if args.clear_slash_commands:
            _clear_commands(token, application_id, guild_id)
            print("slash_commands=cleared")
        if args.send_audit:
            _bot_post(
                token,
                MASTER_CHANNEL_ID,
                "[배포 점검] Hermes Agent v0.18.2가 연결됐습니다. 일반 질문과 승인 명령을 받을 준비가 됐습니다.",
            )
            _bot_post(
                token,
                WORK_CHANNEL_ID,
                "[배포 점검] Hermes → Codex 감사 채널 연결 완료. 아직 Codex 작업은 실행되지 않았습니다.",
            )
            _webhook_post(
                webhook_id,
                webhook_token,
                "[배포 점검] Codex 결과 웹훅 연결 완료. 실제 Codex 작업 결과가 아닌 연결 시험입니다.",
            )
            print("audit_messages=sent")
        return 0
    except (AuditCheckError, LaunchError) as exc:
        print(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

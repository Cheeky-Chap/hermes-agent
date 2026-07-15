from __future__ import annotations

import asyncio
import inspect
import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from hermes_plugin import (  # noqa: E402
    MASTER_CHANNEL_ID,
    WORK_CHANNEL_ID,
    DiscordOrigin,
)
import hermes_plugin as plugin  # noqa: E402


OWNER_ID = "1526740000000000000"
MESSAGE_ID = "1526999999999999999"
JOB_ID = "HC-20260715-ABCD"
SCOPE_HASH = "A" * 64


def discord_event(
    *,
    channel_id: str = MASTER_CHANNEL_ID,
    user_id: str = OWNER_ID,
    message_id: str = MESSAGE_ID,
    text: str = "hello",
    is_bot: bool = False,
):
    source = SimpleNamespace(
        platform=SimpleNamespace(value="discord"),
        chat_id=channel_id,
        user_id=user_id,
        message_id=message_id,
        is_bot=is_bot,
    )
    return SimpleNamespace(source=source, message_id=message_id, text=text)


def close_scheduled(coro):
    coro.close()
    return True


async def immediate_to_thread(func, *args, **kwargs):
    """Run deterministic offline stubs without creating a default executor."""
    return func(*args, **kwargs)


class GatewayPolicyTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(
            plugin.os.environ,
            {"DISCORD_ALLOWED_USERS": OWNER_ID},
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_hooks_are_synchronous(self):
        self.assertFalse(inspect.iscoroutinefunction(plugin._on_pre_gateway_dispatch))
        self.assertFalse(inspect.iscoroutinefunction(plugin._on_pre_tool_call))

    def test_work_channel_is_outbound_only(self):
        event = discord_event(channel_id=WORK_CHANNEL_ID)
        with mock.patch.object(plugin, "_schedule") as schedule:
            result = plugin._on_pre_gateway_dispatch(event=event, gateway=SimpleNamespace())
        self.assertEqual(result["action"], "skip")
        schedule.assert_not_called()

    def test_unauthorized_master_sender_is_rejected(self):
        event = discord_event(user_id="1526740000000000001")
        result = plugin._on_pre_gateway_dispatch(event=event, gateway=SimpleNamespace())
        self.assertEqual(result["action"], "skip")
        self.assertIn("independently authorized", result["reason"])

    def test_bot_and_webhook_messages_are_rejected(self):
        event = discord_event(is_bot=True)
        result = plugin._on_pre_gateway_dispatch(event=event, gateway=SimpleNamespace())
        self.assertEqual(result["action"], "skip")

    def test_slash_command_is_blocked_before_llm(self):
        event = discord_event(text="/restart")
        with mock.patch.object(plugin, "_schedule", side_effect=close_scheduled):
            result = plugin._on_pre_gateway_dispatch(event=event, gateway=SimpleNamespace())
        self.assertEqual(result["action"], "skip")
        self.assertIn("slash", result["reason"])

    def test_exact_control_command_captures_immutable_origin(self):
        event = discord_event(text=f"수정 승인 {JOB_ID}")
        with (
            mock.patch.object(plugin, "_schedule", side_effect=close_scheduled),
            mock.patch.object(plugin, "_handle_command", new=mock.AsyncMock()) as handler,
        ):
            result = plugin._on_pre_gateway_dispatch(event=event, gateway=SimpleNamespace())
        self.assertEqual(result["action"], "skip")
        handler.assert_called_once_with(
            "수정 승인",
            JOB_ID,
            DiscordOrigin(MESSAGE_ID, MASTER_CHANNEL_ID, OWNER_ID),
        )

    def test_control_command_without_snowflake_message_id_fails_closed(self):
        event = discord_event(text=f"승인 {JOB_ID}", message_id="")
        with (
            mock.patch.object(plugin, "_schedule", side_effect=close_scheduled),
            mock.patch.object(plugin, "_handle_command", new=mock.AsyncMock()) as handler,
        ):
            result = plugin._on_pre_gateway_dispatch(event=event, gateway=SimpleNamespace())
        self.assertEqual(result["action"], "skip")
        self.assertIn("replay-safe", result["reason"])
        handler.assert_not_called()

    def test_only_three_answer_policy_tools_are_allowed(self):
        for tool in ("memory", "clarify", "codex_propose"):
            with self.subTest(tool=tool):
                self.assertIsNone(plugin._on_pre_tool_call(tool_name=tool))
        for tool in ("terminal", "write_file", "discord", "web_search", "delegate_task"):
            with self.subTest(tool=tool):
                result = plugin._on_pre_tool_call(tool_name=tool)
                self.assertEqual(result["action"], "block")

    def test_workspace_choices_exclude_self_edit(self):
        self.assertEqual(plugin.WORKSPACES, ("stocks", "tft", "misaka", "daily-report"))
        self.assertNotIn("hermes-broker", plugin.CODEX_PROPOSE_SCHEMA["parameters"]["properties"]["workspace"]["enum"])


class CommandBindingTests(unittest.TestCase):
    def setUp(self):
        self.origin = DiscordOrigin(MESSAGE_ID, MASTER_CHANNEL_ID, OWNER_ID)

    def run_command(self, command):
        with mock.patch.object(plugin.asyncio, "to_thread", side_effect=immediate_to_thread):
            asyncio.run(command)

    def test_apply_fetches_job_then_binds_full_scope_and_origin(self):
        requests = []

        def broker(payload, timeout=plugin.BROKER_TIMEOUT_SECONDS):
            requests.append(payload)
            if payload["op"] == "get":
                return {
                    "ok": True,
                    "result": {
                        "id": JOB_ID,
                        "status": "analysis_ready",
                        "scope_hash": SCOPE_HASH,
                    },
                }
            return {
                "ok": True,
                "result": {
                    "id": JOB_ID,
                    "status": "apply_queued",
                    "scope_hash": SCOPE_HASH,
                },
            }

        with (
            mock.patch.object(plugin, "_broker_request", side_effect=broker),
            mock.patch.object(plugin, "_send_bot", new=mock.AsyncMock(return_value=True)),
            mock.patch.object(plugin, "_schedule", side_effect=close_scheduled),
        ):
            self.run_command(plugin._handle_command("수정 승인", JOB_ID, self.origin))

        common = {
            "job_id": JOB_ID,
            "message_id": MESSAGE_ID,
            "channel_id": MASTER_CHANNEL_ID,
            "user_id": OWNER_ID,
        }
        self.assertEqual(requests[0], {"op": "get", **common})
        self.assertEqual(
            requests[1],
            {"op": "approve_apply", **common, "scope_hash": SCOPE_HASH},
        )

    def test_apply_rejects_short_or_missing_scope_hash(self):
        requests = []

        def broker(payload, timeout=plugin.BROKER_TIMEOUT_SECONDS):
            requests.append(payload)
            return {
                "ok": True,
                "result": {
                    "id": JOB_ID,
                    "status": "analysis_ready",
                    "scope_hash": "ABCD1234",
                },
            }

        send_bot = mock.AsyncMock(return_value=True)
        with (
            mock.patch.object(plugin, "_broker_request", side_effect=broker),
            mock.patch.object(plugin, "_send_bot", new=send_bot),
        ):
            self.run_command(plugin._handle_command("수정 승인", JOB_ID, self.origin))

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["op"], "get")
        self.assertTrue(any("64자리" in call.args[1] for call in send_bot.await_args_list))

    def test_apply_rejects_non_ready_job_even_with_hash(self):
        with (
            mock.patch.object(
                plugin,
                "_broker_request",
                return_value={
                    "ok": True,
                    "result": {"id": JOB_ID, "status": "applied", "scope_hash": SCOPE_HASH},
                },
            ) as broker,
            mock.patch.object(plugin, "_send_bot", new=mock.AsyncMock(return_value=True)),
        ):
            self.run_command(plugin._handle_command("수정 승인", JOB_ID, self.origin))
        self.assertEqual(broker.call_count, 1)

    def test_other_control_requests_always_include_replay_identity(self):
        cases = (
            ("승인", "approve_analysis", "analysis_queued"),
            ("상태", "get", "proposed"),
            ("취소", "cancel", "cancelled"),
        )
        for command, operation, status in cases:
            with self.subTest(command=command):
                requests = []

                def broker(payload, timeout=plugin.BROKER_TIMEOUT_SECONDS):
                    requests.append(payload)
                    return {"ok": True, "result": {"id": JOB_ID, "status": status}}

                with (
                    mock.patch.object(plugin, "_broker_request", side_effect=broker),
                    mock.patch.object(plugin, "_send_bot", new=mock.AsyncMock(return_value=True)),
                    mock.patch.object(plugin, "_schedule", side_effect=close_scheduled),
                ):
                    self.run_command(plugin._handle_command(command, JOB_ID, self.origin))
                self.assertEqual(
                    requests,
                    [{
                        "op": operation,
                        "job_id": JOB_ID,
                        "message_id": MESSAGE_ID,
                        "channel_id": MASTER_CHANNEL_ID,
                        "user_id": OWNER_ID,
                    }],
                )

    def test_terminal_status_retries_result_publication(self):
        for status, result_field in (
            ("analysis_ready", "analysis_result"),
            ("applied", "apply_result"),
        ):
            with self.subTest(status=status):
                job = {"id": JOB_ID, "status": status, result_field: "retained result"}
                with (
                    mock.patch.object(
                        plugin,
                        "_broker_request",
                        return_value={"ok": True, "result": job},
                    ),
                    mock.patch.object(plugin, "_send_bot", new=mock.AsyncMock(return_value=True)),
                    mock.patch.object(plugin, "_publish_terminal", new=mock.AsyncMock()) as publish,
                ):
                    self.run_command(plugin._handle_command("상태", JOB_ID, self.origin))
                publish.assert_awaited_once_with(job)


class PublicationRetryTests(unittest.TestCase):
    def setUp(self):
        plugin._published.clear()
        self.addCleanup(plugin._published.clear)

    def test_send_codex_reports_partial_webhook_failure(self):
        post = mock.Mock(side_effect=(True, False))
        send_bot = mock.AsyncMock(return_value=True)
        with (
            mock.patch.object(plugin, "_chunks", return_value=["first", "second"]),
            mock.patch.object(plugin, "_post_webhook_chunk_sync", new=post),
            mock.patch.object(plugin, "_send_bot", new=send_bot),
            mock.patch.object(plugin.asyncio, "to_thread", side_effect=immediate_to_thread),
        ):
            sent = asyncio.run(plugin._send_codex(JOB_ID, "result", "payload"))
        self.assertFalse(sent)
        self.assertEqual(post.call_count, 2)
        send_bot.assert_awaited_once()

    def test_webhook_credentials_are_rediscovered_for_each_send_attempt(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps([
            {
                "name": plugin.WEBHOOK_NAME,
                "channel_id": WORK_CHANNEL_ID,
                "type": 1,
                "id": "1526999999999999998",
                "token": "test-webhook-credential",
            },
        ]).encode("utf-8")
        with (
            mock.patch.dict(plugin.os.environ, {"DISCORD_BOT_TOKEN": "test-bot-credential"}),
            mock.patch.object(plugin.urllib.request, "urlopen", return_value=response) as urlopen,
        ):
            first = plugin._discover_webhook_sync()
            second = plugin._discover_webhook_sync()
        self.assertEqual(first, second)
        self.assertEqual(urlopen.call_count, 2)
        self.assertFalse(hasattr(plugin, "_webhook_cache"))

    def test_terminal_result_is_only_marked_after_webhook_success(self):
        for status, result_field in (
            ("analysis_ready", "analysis_result"),
            ("applied", "apply_result"),
        ):
            with self.subTest(status=status):
                plugin._published.clear()
                job = {
                    "id": JOB_ID,
                    "status": status,
                    "version": 7,
                    result_field: "retained result",
                }
                marker = (JOB_ID, status, 7)
                send_codex = mock.AsyncMock(side_effect=(False, True))
                with (
                    mock.patch.object(plugin, "_send_codex", new=send_codex),
                    mock.patch.object(plugin, "_send_bot", new=mock.AsyncMock(return_value=True)),
                ):
                    asyncio.run(plugin._publish_terminal(job))
                    self.assertNotIn(marker, plugin._published)
                    asyncio.run(plugin._publish_terminal(job))
                    self.assertIn(marker, plugin._published)
                    asyncio.run(plugin._publish_terminal(job))
                self.assertEqual(send_codex.await_count, 2)


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(
            plugin.os.environ,
            {"DISCORD_ALLOWED_USERS": OWNER_ID},
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def session_modules(self, *, message_id=MESSAGE_ID):
        values = {
            "HERMES_SESSION_PLATFORM": "discord",
            "HERMES_SESSION_CHAT_ID": MASTER_CHANNEL_ID,
            "HERMES_SESSION_USER_ID": OWNER_ID,
            "HERMES_SESSION_MESSAGE_ID": message_id,
        }
        gateway = types.ModuleType("gateway")
        gateway.__path__ = []
        session_context = types.ModuleType("gateway.session_context")
        session_context.get_session_env = lambda name, default="": values.get(name, default)
        return {"gateway": gateway, "gateway.session_context": session_context}

    def test_submit_binds_origin_and_preserves_four_workspaces(self):
        for workspace in plugin.WORKSPACES:
            with self.subTest(workspace=workspace):
                requests = []

                def broker(payload, timeout=plugin.BROKER_TIMEOUT_SECONDS):
                    requests.append(payload)
                    return {
                        "ok": True,
                        "result": {
                            "id": JOB_ID,
                            "status": "proposed",
                            "workspace": workspace,
                            "objective": "inspect safely",
                        },
                    }

                with (
                    mock.patch.dict(sys.modules, self.session_modules()),
                    mock.patch.object(plugin, "_broker_request", side_effect=broker),
                    mock.patch.object(plugin, "_schedule", side_effect=close_scheduled),
                ):
                    result = json.loads(plugin._handle_codex_propose({
                        "objective": "inspect safely",
                        "workspace": workspace,
                    }))
                self.assertTrue(result["ok"])
                self.assertEqual(requests[0]["message_id"], MESSAGE_ID)
                self.assertEqual(requests[0]["channel_id"], MASTER_CHANNEL_ID)
                self.assertEqual(requests[0]["user_id"], OWNER_ID)

    def test_submit_may_omit_unavailable_session_message_id(self):
        requests = []

        def broker(payload, timeout=plugin.BROKER_TIMEOUT_SECONDS):
            requests.append(payload)
            return {
                "ok": True,
                "result": {
                    "id": JOB_ID,
                    "status": "proposed",
                    "workspace": "stocks",
                    "objective": "inspect safely",
                },
            }

        with (
            mock.patch.dict(sys.modules, self.session_modules(message_id="")),
            mock.patch.object(plugin, "_broker_request", side_effect=broker),
            mock.patch.object(plugin, "_schedule", side_effect=close_scheduled),
        ):
            result = json.loads(plugin._handle_codex_propose({
                "objective": "inspect safely",
                "workspace": "stocks",
            }))
        self.assertTrue(result["ok"])
        self.assertNotIn("message_id", requests[0])
        self.assertEqual(requests[0]["channel_id"], MASTER_CHANNEL_ID)
        self.assertEqual(requests[0]["user_id"], OWNER_ID)

    def test_self_edit_workspace_is_rejected_without_broker_request(self):
        with (
            mock.patch.dict(sys.modules, self.session_modules()),
            mock.patch.object(plugin, "_broker_request") as broker,
        ):
            result = json.loads(plugin._handle_codex_propose({
                "objective": "edit the bridge",
                "workspace": "hermes-broker",
            }))
        self.assertFalse(result["ok"])
        broker.assert_not_called()


class SocketProtocolTests(unittest.TestCase):
    class FakeSocket:
        def __init__(self, chunks):
            self.chunks = list(chunks)
            self.timeout = None
            self.closed = False

        def settimeout(self, value):
            self.timeout = value

        def connect(self, path):
            self.path = path

        def sendall(self, data):
            self.sent = data

        def recv(self, size):
            return self.chunks.pop(0) if self.chunks else b""

        def close(self):
            self.closed = True

    def test_unterminated_response_fails_closed(self):
        fake = self.FakeSocket([b'{"ok":true}'])
        with mock.patch.object(plugin.socket, "socket", return_value=fake):
            result = plugin._broker_request({"op": "ping"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "broker_unavailable")
        self.assertTrue(fake.closed)

    def test_response_without_boolean_ok_is_rejected(self):
        fake = self.FakeSocket([b'{"ok":"yes"}\n'])
        with mock.patch.object(plugin.socket, "socket", return_value=fake):
            result = plugin._broker_request({"op": "ping"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "bad_response")

    def test_oversized_request_never_opens_socket(self):
        with mock.patch.object(plugin.socket, "socket") as socket_factory:
            result = plugin._broker_request({"op": "ping", "padding": "x" * plugin.MAX_BROKER_REQUEST_BYTES})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "request_too_large")
        socket_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()

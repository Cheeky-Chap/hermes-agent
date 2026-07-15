from __future__ import annotations

import importlib.util
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SERVICE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path("/opt/data/state/hermes-master/home/config.yaml")
LAUNCHER_PATH = SERVICE_DIR / "launch_container.py"

SPEC = importlib.util.spec_from_file_location("hermes_container_launcher", LAUNCHER_PATH)
if SPEC is None or SPEC.loader is None:  # pragma: no cover - import machinery guard
    raise RuntimeError("could not load launch_container.py")
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class DotenvTests(unittest.TestCase):
    def test_loads_only_required_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "HERMES_DISCORD_BOT_TOKEN='discord-test-value'\n"
                "DEEPSEEK_API_KEY=deepseek-test-value # local comment\n"
                "DISCORD_ALLOWED_USERS=123456789012345678\n"
                "UNRELATED_SECRET=must-not-be-loaded\n",
                encoding="utf-8",
            )
            path.chmod(0o600)
            selected = launcher.load_selected_env(path)

        self.assertEqual(set(selected), set(launcher.REQUIRED_SOURCE_KEYS))
        self.assertNotIn("UNRELATED_SECRET", selected)

    def test_rejects_group_readable_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "HERMES_DISCORD_BOT_TOKEN=x\n"
                "DEEPSEEK_API_KEY=y\n"
                "DISCORD_ALLOWED_USERS=123456789012345678\n",
                encoding="utf-8",
            )
            path.chmod(0o640)
            with self.assertRaises(launcher.LaunchError):
                launcher.load_selected_env(path)


class DockerArgvTests(unittest.TestCase):
    def setUp(self) -> None:
        self.selected = {
            "HERMES_DISCORD_BOT_TOKEN": "discord-test-value",
            "DEEPSEEK_API_KEY": "deepseek-test-value",
            "DISCORD_ALLOWED_USERS": "123456789012345678",
        }

    def test_is_pinned_isolated_and_secret_safe(self) -> None:
        with mock.patch.object(launcher, "load_selected_env", return_value=self.selected):
            argv, child_env = launcher.build_docker_argv()

        joined = "\n".join(argv)
        self.assertEqual(
            launcher.IMAGE,
            "nousresearch/hermes-agent@sha256:"
            "3db34ce19adfa080736a2a3feb0316dbcccc588faa9afe7fd8ae1c03b4f1a53a",
        )
        self.assertEqual(argv[-3:], [launcher.IMAGE, "gateway", "run"])
        self.assertIn("--pull\nnever", joined)
        self.assertIn("--restart\nno", joined)
        self.assertNotIn("discord-test-value", joined)
        self.assertNotIn("deepseek-test-value", joined)
        self.assertIn("DISCORD_BOT_TOKEN", argv)
        self.assertIn("DEEPSEEK_API_KEY", argv)
        self.assertEqual(child_env["DISCORD_BOT_TOKEN"], "discord-test-value")
        self.assertEqual(child_env["DEEPSEEK_API_KEY"], "deepseek-test-value")

        mounts = [argv[index + 1] for index, item in enumerate(argv) if item == "--mount"]
        self.assertEqual(
            mounts,
            [
                f"type=bind,src={launcher.HOME_PATH},dst=/opt/data",
                (
                    f"type=bind,src={launcher.PLUGIN_PATH},"
                    "dst=/opt/data/plugins/hermes-codex-bridge,readonly"
                ),
                f"type=bind,src={launcher.IPC_PATH},dst=/run/hermes-codex,readonly",
            ],
        )
        self.assertNotIn("/opt/data/projects/stocks", joined)
        self.assertNotIn("/opt/data/projects/tft", joined)
        self.assertNotIn("/var/run/docker.sock", joined)
        self.assertNotIn("--privileged", argv)
        self.assertNotIn("--publish", argv)
        self.assertNotIn("--user", argv)
        self.assertNotIn("--read-only", argv)
        self.assertNotIn("host", [argv[index + 1] for index, item in enumerate(argv) if item == "--network"])

    def test_capabilities_and_healthcheck_are_fail_closed(self) -> None:
        with mock.patch.object(launcher, "load_selected_env", return_value=self.selected):
            argv, _child_env = launcher.build_docker_argv()

        added_caps = {argv[index + 1] for index, item in enumerate(argv) if item == "--cap-add"}
        self.assertEqual(
            added_caps,
            {"CHOWN", "DAC_OVERRIDE", "FOWNER", "KILL", "SETGID", "SETUID"},
        )
        self.assertIn("ALL", [argv[index + 1] for index, item in enumerate(argv) if item == "--cap-drop"])
        self.assertIn("no-new-privileges:true", argv)
        health = argv[argv.index("--health-cmd") + 1]
        self.assertEqual(health, launcher.HEALTH_COMMAND)
        self.assertIn('gateway_state\")==\"running', health)
        self.assertIn('get(\"discord\",{})', health)
        self.assertIn('/proc/{p}', health)
        self.assertIn("DISCORD_COMMAND_SYNC_POLICY=off", argv)

    def test_docker_failure_does_not_echo_captured_output(self) -> None:
        completed = mock.Mock(returncode=1, stdout="", stderr="discord-test-value")
        with (
            mock.patch.object(launcher, "local_checks", return_value=[("ready", True)]),
            mock.patch.object(launcher, "container_exists", return_value=False),
            mock.patch.object(
                launcher,
                "build_docker_argv",
                return_value=(["docker", "run", launcher.IMAGE], {"TOKEN": "discord-test-value"}),
            ),
            mock.patch.object(launcher.subprocess, "run", return_value=completed),
        ):
            with self.assertRaises(launcher.LaunchError) as raised:
                launcher.create_container()

        self.assertNotIn("discord-test-value", str(raised.exception))


class ConfigTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("yaml"), "PyYAML not installed")
    def test_discord_surface_is_bounded(self) -> None:
        import yaml

        config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(config["model"], {"provider": "deepseek", "default": "deepseek-v4-pro"})
        self.assertTrue(config["group_sessions_per_user"])
        self.assertEqual(config["discord"]["allowed_channels"], launcher.MASTER_CHANNEL)
        self.assertEqual(config["discord"]["ignored_channels"], launcher.WORK_CHANNEL)
        self.assertFalse(config["platforms"]["discord"]["extra"]["slash_commands"])
        self.assertTrue(config["platforms"]["discord"]["extra"]["group_sessions_per_user"])
        self.assertEqual(
            config["platform_toolsets"]["discord"],
            ["memory", "clarify", "hermes_codex_bridge", "no_mcp"],
        )
        self.assertEqual(config["approvals"]["mode"], "manual")
        self.assertEqual(config["approvals"]["deny"], ["*"])
        self.assertIn("terminal", config["agent"]["disabled_toolsets"])
        self.assertIn("file", config["agent"]["disabled_toolsets"])
        self.assertIn("discord_admin", config["agent"]["disabled_toolsets"])


class CheckScriptTests(unittest.TestCase):
    def test_start_check_does_not_compile_bytecode(self) -> None:
        script = (SERVICE_DIR / "start.sh").read_text(encoding="utf-8")
        self.assertNotIn("py_compile", script)
        self.assertIn("python3 -B", script)

    def test_operator_files_have_restrictive_modes(self) -> None:
        for name in (
            "launch_container.py",
            "start.sh",
            "run-background.sh",
            "ensure-running.sh",
            "status.sh",
            "stop.sh",
        ):
            self.assertEqual(stat.S_IMODE((SERVICE_DIR / name).stat().st_mode), 0o750)
        self.assertEqual(
            stat.S_IMODE(launcher.HOME_PATH.stat().st_mode),
            0o700,
        )
        self.assertIn(stat.S_IMODE(CONFIG_PATH.stat().st_mode), {0o600, 0o640})


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

import codex_runner as runner  # noqa: E402


class SecretScannerTests(unittest.TestCase):
    def test_known_env_and_auth_values_are_detected_without_exposing_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env_secret = b"unit-env-secret-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            auth_secret = "unit-auth-secret-1234567890ABCDEFGHIJ"
            env_path = root / ".env"
            auth_path = root / "auth.json"
            env_path.write_bytes(b"DEEPSEEK_API_KEY=" + env_secret + b"\nPUBLIC_FLAG=true\n")
            auth_path.write_text(
                json.dumps({"tokens": {"access_token": auth_secret}}),
                encoding="utf-8",
            )

            scanner = runner.SecretScanner.from_known_sources(
                env_paths=(env_path,),
                auth_paths=(auth_path,),
                include_process_env=False,
            )

            self.assertEqual(scanner.scan_bytes(env_secret), "env:DEEPSEEK_API_KEY")
            self.assertEqual(
                scanner.scan_bytes(base64.b64encode(env_secret)),
                "env:DEEPSEEK_API_KEY",
            )
            self.assertEqual(scanner.scan_text(auth_secret), "auth.tokens.access_token")
            self.assertIsNone(scanner.scan_text("ordinary safe output"))


class StagingCopyTests(unittest.TestCase):
    def test_secret_files_and_all_symlinks_are_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            destination = root / "destination"
            outside = root / "outside.txt"
            source.mkdir()
            (source / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
            (source / ".env").write_text("TOKEN=fake-secret-value\n", encoding="utf-8")
            outside.write_text("outside\n", encoding="utf-8")
            os.symlink(outside, source / "outside-link")
            os.symlink(root, source / "state")

            runner.copy_workspace_for_staging(source, destination)

            self.assertEqual((destination / "module.py").read_text(encoding="utf-8"), "VALUE = 1\n")
            self.assertFalse((destination / ".env").exists())
            self.assertFalse((destination / "outside-link").exists())
            self.assertFalse((destination / "state").exists())
            self.assertEqual(set(runner.snapshot_tree(source)), {"module.py"})


class BubblewrapPolicyTests(unittest.TestCase):
    def test_argv_mounts_only_minimal_host_paths_and_enables_landlock(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            argv = runner.build_bwrap_argv(workspace, write=False, auth_fd=77)

        readonly_mounts = {
            (argv[index + 1], argv[index + 2])
            for index, value in enumerate(argv[:-2])
            if value == "--ro-bind"
        }
        self.assertNotIn(("/", "/"), readonly_mounts)
        self.assertNotIn((str(runner.CODEX_HOME), str(runner.CODEX_HOME)), readonly_mounts)
        self.assertIn(("/usr", "/usr"), readonly_mounts)
        self.assertIn((str(runner.CODEX_BIN.resolve()), runner.SANDBOX_CODEX_BIN), readonly_mounts)
        self.assertIn((str(workspace.resolve()), "/work"), readonly_mounts)
        self.assertNotIn("/opt/data", argv)
        self.assertIn("--strict-config", argv)
        self.assertIn("--ignore-user-config", argv)
        self.assertIn("--ephemeral", argv)
        feature_pairs = set(zip(argv, argv[1:]))
        self.assertIn(("--enable", "use_legacy_landlock"), feature_pairs)
        self.assertIn(("--file", "77"), feature_pairs)
        code_home_index = argv.index("CODEX_HOME")
        self.assertEqual(argv[code_home_index + 1], runner.SANDBOX_CODEX_HOME)


class StreamLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_drains_but_caps_retained_output(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"x" * 1024)
        reader.feed_eof()
        overflow_event = asyncio.Event()

        capture = await runner._read_limited(reader, 64, overflow_event)

        self.assertTrue(capture.overflow)
        self.assertEqual(len(capture.data), 64)
        self.assertTrue(overflow_event.is_set())


if __name__ == "__main__":
    unittest.main()

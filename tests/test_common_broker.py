from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

import broker  # noqa: E402
from codex_runner import RunnerError, copy_workspace_for_staging, workspace_fingerprint  # noqa: E402
from common import compute_scope_hash  # noqa: E402


class ScopeHashTests(unittest.TestCase):
    def test_scope_hash_is_full_and_bound_to_file_record(self) -> None:
        job = {
            "id": "HC-20260715-ABCD",
            "workspace": "stocks",
            "objective": "update one parser",
            "analysis_result": "analysis",
            "workspace_fingerprint": "1" * 64,
            "scope_entries": [
                {
                    "path": "parser.py",
                    "operation": "modify",
                    "sha256": "2" * 64,
                    "size": 10,
                    "mode": 0o660,
                }
            ],
        }
        first = compute_scope_hash(job)
        self.assertRegex(first, r"^[A-F0-9]{64}$")
        job["scope_entries"][0]["sha256"] = "3" * 64
        self.assertNotEqual(first, compute_scope_hash(job))


class ApplyTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.live = self.root / "live"
        self.stage = self.root / "stage"
        self.backups = self.root / "backups"
        self.live.mkdir()
        (self.live / "one.py").write_text("old one\n", encoding="utf-8")
        (self.live / "two.py").write_text("old two\n", encoding="utf-8")
        os.chmod(self.live / "one.py", 0o660)
        os.chmod(self.live / "two.py", 0o660)
        copy_workspace_for_staging(self.live, self.stage)
        self.fingerprint = workspace_fingerprint(self.live)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _job(self, paths: list[str]) -> dict:
        return {
            "id": "HC-20260715-ABCD",
            "version": 7,
            "scope_hash": "A" * 64,
            "scope_paths": paths,
        }

    def test_commit_is_journaled_and_preserves_mode_and_xattr(self) -> None:
        xattr_supported = True
        try:
            os.setxattr(self.live / "one.py", "user.hermes_test", b"preserve")
            copy_workspace_for_staging(self.live, self.stage)
            self.fingerprint = workspace_fingerprint(self.live)
        except OSError:
            xattr_supported = False
        (self.stage / "one.py").write_text("new one\n", encoding="utf-8")
        changed = broker.apply_staged_changes(
            self._job(["one.py"]),
            self.live,
            self.stage,
            self.fingerprint,
            backup_root=self.backups,
        )
        self.assertEqual(changed, ["one.py"])
        self.assertEqual((self.live / "one.py").read_text(encoding="utf-8"), "new one\n")
        self.assertEqual((self.live / "one.py").stat().st_mode & 0o777, 0o660)
        if xattr_supported:
            self.assertEqual(os.getxattr(self.live / "one.py", "user.hermes_test"), b"preserve")
        manifest_path = next(self.backups.rglob("manifest.json"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "committed")
        self.assertEqual((manifest_path.parent / "files/one.py").read_text(encoding="utf-8"), "old one\n")

    def test_out_of_scope_change_is_rejected_before_live_write(self) -> None:
        (self.stage / "two.py").write_text("not approved\n", encoding="utf-8")
        with self.assertRaisesRegex(RunnerError, "outside the approved scope"):
            broker.apply_staged_changes(
                self._job(["one.py"]),
                self.live,
                self.stage,
                self.fingerprint,
                backup_root=self.backups,
            )
        self.assertEqual((self.live / "two.py").read_text(encoding="utf-8"), "old two\n")
        self.assertFalse(self.backups.exists())

    def test_live_drift_blocks_apply(self) -> None:
        (self.stage / "one.py").write_text("approved\n", encoding="utf-8")
        (self.live / "two.py").write_text("concurrent drift\n", encoding="utf-8")
        with self.assertRaisesRegex(RunnerError, "workspace changed"):
            broker.apply_staged_changes(
                self._job(["one.py"]),
                self.live,
                self.stage,
                self.fingerprint,
                backup_root=self.backups,
            )
        self.assertEqual((self.live / "one.py").read_text(encoding="utf-8"), "old one\n")

    def test_partial_apply_is_rolled_back(self) -> None:
        (self.stage / "one.py").write_text("new one\n", encoding="utf-8")
        (self.stage / "two.py").write_text("new two\n", encoding="utf-8")
        real_copy = broker._copy_atomic
        failed = False

        def fail_second_stage_copy(source: Path, destination: Path, *args, **kwargs):
            nonlocal failed
            if source.parent == self.stage and destination == self.live / "two.py" and not failed:
                failed = True
                raise OSError("injected apply failure")
            return real_copy(source, destination, *args, **kwargs)

        with mock.patch.object(broker, "_copy_atomic", side_effect=fail_second_stage_copy):
            with self.assertRaises(OSError):
                broker.apply_staged_changes(
                    self._job(["one.py", "two.py"]),
                    self.live,
                    self.stage,
                    self.fingerprint,
                    backup_root=self.backups,
                )
        self.assertEqual((self.live / "one.py").read_text(encoding="utf-8"), "old one\n")
        self.assertEqual((self.live / "two.py").read_text(encoding="utf-8"), "old two\n")
        manifest = json.loads(next(self.backups.rglob("manifest.json")).read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "rolled_back")


if __name__ == "__main__":
    unittest.main()

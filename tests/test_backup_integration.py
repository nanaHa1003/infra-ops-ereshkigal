"""Real restic round trip, using a temporary local repository and fake secrets."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("restic"), "restic binary required for local integration test")
class ResticRoundTrip(unittest.TestCase):
    def test_upload_restore_retention_preserves_other_backup_sets(self):
        sys.path.insert(0, str(ROOT / "roles/backup/files"))
        runner = importlib.import_module("backup_runner")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            source = root / "config.yml"
            source.write_text("restorable configuration")
            repo = root / "repository"
            config = {"host": "fixture", "state_root": str(root / "state"),
                      "repositories": {"database": {"url": str(repo), "access_key_id": "fake", "secret_access_key": "fake", "restic_password": "test-only"}},
                      "jobs": {"database": {"repository": "database", "sources": [{"id": "config", "kind": "files", "path": str(source)}],
                                            "retention": {"within": "1h", "daily": 1, "weekly": 1, "monthly": 1}}}}
            env = {**os.environ, "RESTIC_REPOSITORY": str(repo), "RESTIC_PASSWORD": "test-only", "RESTIC_CACHE_DIR": str(root / "cache")}
            def cli(*args, cwd=None):
                return subprocess.run(["restic", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout
            with patch.dict(os.environ, {"RESTIC_CACHE_DIR": str(root / "cache")}):
                probe = subprocess.run(["restic", "snapshots", "--json"], env=env, capture_output=True)
                if probe.returncode == 10:
                    self.assertTrue(runner.ensure_initialized(config, "database"))
                else:
                    # Older distro restic uses ambiguous exit 1 for an absent repo.
                    # Production ensure-init deliberately refuses to infer absence.
                    cli("init")
                self.assertFalse(runner.ensure_initialized(config, "database"))
            stage = root / "state/staging/database"
            stage.mkdir(parents=True)
            (stage / "old").write_text("old")
            for days in (60, 59):
                timestamp = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
                cli("backup", "--host", "fixture", "--tag", "homelab-config-db:database", "--time", timestamp, ".", cwd=stage)
            cli("backup", "--host", "fixture", "--tag", "unrelated-job", str(source))
            with patch.dict(os.environ, {"RESTIC_CACHE_DIR": str(root / "cache")}), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(runner.run_job(config, "database"), 0)
                self.assertEqual(runner.maintenance(config, "database", preview=True), 0)
            self.assertEqual(len(json.loads(cli("snapshots", "--json"))), 4)
            with patch.dict(os.environ, {"RESTIC_CACHE_DIR": str(root / "cache")}), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(runner.maintenance(config, "database"), 0)
            snapshots = json.loads(cli("snapshots", "--json"))
            self.assertEqual(len(snapshots), 2)
            self.assertTrue(any(snapshot["tags"] == ["unrelated-job"] for snapshot in snapshots))
            restored = root / "restored"
            cli("restore", "latest", "--tag", "homelab-config-db:database", "--target", str(restored))
            files = list(restored.rglob("config.yml"))
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].read_text(), "restorable configuration")
            with patch.dict(os.environ, {"RESTIC_CACHE_DIR": str(root / "cache")}), redirect_stdout(io.StringIO()):
                restored_by_runner = runner.restore_job(config, "database", "latest")
            self.assertEqual(next(restored_by_runner.rglob("config.yml")).read_text(), "restorable configuration")
            other_id = next(snapshot["id"] for snapshot in snapshots if snapshot["tags"] == ["unrelated-job"])
            with patch.dict(os.environ, {"RESTIC_CACHE_DIR": str(root / "cache")}):
                with self.assertRaises(runner.BackupError):
                    runner.restore_job(config, "database", other_id)
            stage.mkdir(parents=True)
            (stage / "incomplete").write_text("not an accepted backup")
            pending_output = cli("backup", "--json", "--host", "fixture", "--tag", "homelab-pending:database", ".", cwd=stage)
            pending_id = next(item["snapshot_id"] for item in map(json.loads, pending_output.splitlines()) if item.get("message_type") == "summary")
            with patch.dict(os.environ, {"RESTIC_CACHE_DIR": str(root / "cache")}):
                with self.assertRaises(runner.BackupError):
                    runner.restore_job(config, "database", pending_id)
            cli("check", "--read-data")

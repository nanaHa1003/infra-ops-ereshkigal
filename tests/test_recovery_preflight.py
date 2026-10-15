"""Preflight refuses unsafe hosts before bootstrap or artifact installation."""
import importlib
import hashlib
import json
import tempfile
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "roles/recovery/files"), str(ROOT / "roles/backup/files")]

class RecoveryPreflight(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "roles/recovery/files/recovery_runner.py").exists(), "missing recovery preflight")
        self.module = importlib.import_module("recovery_runner")
        self.config = {"target": {"distribution": "Fedora", "release": "43", "architecture": "amd64", "machine_id": "target-one", "min_free_bytes": 100,
            "probe": {"mount_ok": True, "free_bytes": 1000, "package_conflicts": [], "nonempty_roots": [], "conflicting_services": [], "active_backup_timers": False, "ddns_active": False, "restic": False}}}

    def test_missing_mount_low_space_architecture_and_podman_block(self):
        for key, value, code in (("mount_ok", False, "storage_mount_missing"), ("free_bytes", 1, "insufficient_free_space"),
                                  ("package_conflicts", ["podman"], "conflicting_container_packages")):
            with self.subTest(key=key):
                config = {"target": {**self.config["target"], "probe": {**self.config["target"]["probe"], key: value}}}
                issues = self.module.preflight_issues(config)
                self.assertIn(code, [item["code"] for item in issues])
        self.config["target"]["architecture"] = "arm64"
        self.assertIn("unsupported_target_platform", [row["code"] for row in self.module.preflight_issues(self.config)])

    def test_plan_without_remote_restic_uses_controller_or_reports_incomplete(self):
        with patch.object(self.module.shutil, "which", return_value="/fixture/restic"):
            self.assertNotIn("source_tool_unavailable", [row["code"] for row in self.module.preflight_issues(self.config)])
        with patch.object(self.module.shutil, "which", return_value=None):
            self.assertIn("source_tool_unavailable", [row["code"] for row in self.module.preflight_issues(self.config)])

    def test_state_root_is_bound_to_the_plan_profile(self):
        config = {"source": {"repository": "db", "host": "old", "job": "db", "staging_path": "/old/staging"},
            "repository": {"url": "/fixture/repo"}, "services": ["homepage"], "state_root": "/var/lib/recovery-one",
            "target": {"host": "new", "machine_id": "new-machine", "storage_root": "/srv/data", "stack_root": "/opt/stack", "storage_mount": "/srv", "min_free_bytes": 100,
                       "distribution": "Fedora", "release": "43", "architecture": "amd64"}, "registry": {}}
        original = self.module.profile_hash(config)
        config["state_root"] = "/var/lib/recovery-two"
        self.assertNotEqual(self.module.profile_hash(config), original)

    def test_download_rejects_corrupt_artifact_before_reserving_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            destination = root / "data/homepage/config"
            selection = {"snapshot_id": "a" * 64, "tree_root": "/", "manifest_path": "/manifest.json"}
            source = {"kind": "files", "artifacts": [{"relative_path": "homepage-config/settings.yml", "entry_type": "file", "size": 8, "sha256": hashlib.sha256(b"expected").hexdigest()}]}
            model = {"selection": selection, "services": ["homepage"], "sources": {"homepage-config": source}}
            target = {"machine_id": "new-machine", "storage_root": str(root / "data"), "storage_mount": "/"}
            plan = {"run_id": "corrupt-run", "selection": selection, "model": model, "target": target, "profile_sha256": "b" * 64,
                    "services": ["homepage"], "approved_roots": [str(destination)], "image_variables": {}}
            config = {"state_root": str(root / "state"), "target": target, "services": ["homepage"], "source": {}, "repository": {}, "secrets": {},
                "registry": {"homepage": {"sources": {"homepage-config": {"kind": "files", "layout": "file", "target": "homepage/config/settings.yml"}}}}}
            def restore(*args, **kwargs):
                path = root / "state/runs/corrupt-run/scratch/homepage-config"
                path.mkdir(parents=True, exist_ok=True)
                (path / "settings.yml").write_text("corrupt")
                return b""
            with patch.object(self.module, "restic", side_effect=restore), patch.object(self.module, "select_snapshot", return_value=selection):
                with self.assertRaises(self.module.BackupError): self.module.download(config, plan)
            state = json.loads((root / "state/runs/corrupt-run/state.json").read_text())
            self.assertEqual(state["owned_paths"], [])
            self.assertFalse(destination.exists())

    def test_unreadable_container_or_timer_state_is_not_assumed_empty(self):
        self.config["target"]["probe"]["service_probe_failed"] = True
        self.assertIn("target_service_state_unknown", [row["code"] for row in self.module.preflight_issues(self.config)])

    def test_background_jobs_block_clean_host_restore(self):
        self.config["target"]["probe"]["active_backup_timers"] = True
        self.config["target"]["probe"]["ddns_active"] = True
        codes = [item["code"] for item in self.module.preflight_issues(self.config)]
        self.assertIn("backup_timers_already_active", codes)
        self.assertIn("ddns_already_active", codes)

if __name__ == "__main__": unittest.main()

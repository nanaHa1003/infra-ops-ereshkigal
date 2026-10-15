"""Real restic recovery after the source data has disappeared; no B2 calls."""
import importlib
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
sys.path.insert(0, str(ROOT / "roles/backup/files"))
sys.path.insert(0, str(ROOT / "roles/recovery/files"))

@unittest.skipUnless(shutil.which("restic"), "restic required for real repository test")
class RecoveryRestic(unittest.TestCase):
    def test_old_host_restores_to_new_root_without_source_and_excludes_other_snapshots(self):
        module = importlib.import_module("recovery_manifest")
        common = importlib.import_module("backup_common")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            repo = {"url": str(root / "repository"), "restic_password": "fixture-only", "access_key_id": "fixture-key", "secret_access_key": "fixture-secret"}
            stage = root / "old/state/staging/database"
            stage.mkdir(parents=True)
            (stage / "manifest.json").write_text(json.dumps({"completed_exports_at": "2026-10-15T09:00:00Z", "sources": {"config": {"kind": "files"}}}))
            (stage / "config").mkdir()
            (stage / "config/settings.yml").write_text("from-old-server")
            with patch.dict(os.environ, {"RESTIC_CACHE_DIR": str(root / "cache")}):
                common.restic(repo, ["init"])
                for host, tag in [("old.example", "homelab-config-db:database"), ("new.example", "homelab-config-db:database"),
                                  ("old.example", "homelab-config-db:other"), ("old.example", "homelab-pending:database")]:
                    common.restic(repo, ["backup", "--host", host, "--tag", tag, "."], cwd=stage)
                before = json.loads(common.restic(repo, ["snapshots", "--json"], capture=True))
                scope = {"repository": "database", "host": "old.example", "job": "database", "staging_path": str(stage), "snapshot": "latest"}
                selection = module.select_snapshot(repo, scope)
                expected = next(item["id"] for item in before if item["hostname"] == "old.example" and item["tags"] == ["homelab-config-db:database"])
                self.assertEqual(selection["snapshot_id"], expected)
                runner = importlib.import_module("recovery_runner")
                config = {"source": scope, "repository": repo, "state_root": str(root / "target-state"), "services": ["homepage"],
                    "plan_file": str(root / "controller/plan.json"), "domain": "fixture.example", "secrets": {},
                    "legacy_profile": {"sources": {"config": {"kind": "files"}}, "images": {"homepage": [{"service": "homepage", "image": "homepage:fixture", "registry_digests": ["homepage@sha256:" + "d" * 64], "architecture": "amd64", "os": "linux"}]}},
                    "registry": {"homepage": {"sources": {"config": {"kind": "files", "layout": "directory", "target": "homepage/config"}}}},
                    "target": {"host": "replacement", "machine_id": "new-machine", "distribution": "Fedora", "release": "43", "architecture": "amd64",
                        "storage_root": "/srv/replacement", "stack_root": "/opt/recovered", "storage_mount": "/srv", "min_free_bytes": 1,
                        "probe": {"mount_ok": True, "free_bytes": 10000}}}
                result = runner.create_plan(config)
                self.assertEqual(result["blockers"], [])
                self.assertEqual(result["snapshot_id"], expected)
                public_plan = (root / "controller/plan.json").read_text()
                self.assertNotIn("fixture-secret", public_plan)
                self.assertNotIn("fixture-only", public_plan)
                self.assertEqual((root / "controller/plan.json").stat().st_mode & 0o777, 0o600)
                shutil.rmtree(root / "old")
                target = root / "replacement/restored"
                common.restic(repo, ["restore", selection["snapshot_id"], "--target", str(target), "--verify"])
                paths = list(target.rglob("settings.yml"))
                self.assertEqual(len(paths), 1)
                self.assertEqual(paths[0].read_text(), "from-old-server")
                after = json.loads(common.restic(repo, ["snapshots", "--json"], capture=True))
                self.assertEqual({item["id"] for item in before}, {item["id"] for item in after})

if __name__ == "__main__":
    unittest.main()

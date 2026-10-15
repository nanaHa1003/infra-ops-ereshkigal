"""Recovery metadata must describe real staged artifacts without credentials."""
import hashlib
import importlib
import json
from pathlib import Path
import sys
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

FILES = Path(__file__).resolve().parents[1] / "roles/backup/files"

class BackupManifest(unittest.TestCase):
    def setUp(self):
        self.assertTrue((FILES / "backup_manifest.py").exists(), "missing manifest producer")
        sys.path.insert(0, str(FILES))
        self.module = importlib.import_module("backup_manifest")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stage = Path(self.temp.name).resolve()
        (self.stage / "config/empty").mkdir(parents=True)
        (self.stage / "config/settings.yml").write_text("enabled: true")
        self.config = {"host": "old-host", "job_id": "database",
            "deployment": {"revision": "a" * 40, "dirty": False, "vault_revision": "b" * 40},
            "repositories": {"database": {"restic_password": "never-copy-this-secret"}},
            "jobs": {"database": {"sources": [{"id": "config", "kind": "files", "path": "/old/config"}]}}}

    def test_v2_manifest_records_artifacts_and_omits_secrets(self):
        manifest = self.module.build_manifest(self.stage, self.config, {"config": {"kind": "files"}})
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual((manifest["host"], manifest["job"]), ("old-host", "database"))
        entries = {item["relative_path"]: item for item in manifest["sources"]["config"]["artifacts"]}
        file = entries["config/settings.yml"]
        self.assertEqual(file["sha256"], hashlib.sha256(b"enabled: true").hexdigest())
        self.assertEqual(file["size"], 13)
        self.assertEqual(entries["config/empty"]["entry_type"], "directory")
        self.assertNotIn("never-copy-this-secret", json.dumps(manifest))

    def test_manifest_tracks_all_service_images_not_only_database(self):
        self.config["jobs"]["database"]["service_projects"] = {"n8n": "n8n"}
        images = [{"image": "postgres:18-alpine", "registry_digests": ["postgres@sha256:" + "a" * 64], "architecture": "amd64"},
                  {"image": "n8n:test", "registry_digests": ["n8n@sha256:" + "b" * 64], "architecture": "amd64"}]
        with patch.object(self.module, "capture_project_images", return_value=images):
            manifest = self.module.build_manifest(self.stage, self.config, {"config": {"kind": "files"}})
        self.assertEqual([item["image"] for item in manifest["images"]["n8n"]], ["postgres:18-alpine", "n8n:test"])

    def test_missing_registry_digest_is_reported_not_replaced_by_latest(self):
        self.config["jobs"]["database"]["service_projects"] = {"proxy": "proxy"}
        with patch.object(self.module, "capture_project_images", return_value=[{"image": "local:caddy", "registry_digests": [], "architecture": "amd64"}]):
            manifest = self.module.build_manifest(self.stage, self.config, {"config": {"kind": "files"}})
        self.assertIn("image_digest_unavailable:proxy", manifest["limitations"])
        self.assertEqual(manifest["images"]["proxy"][0]["image"], "local:caddy")

    def test_backup_does_not_require_server_git_checkout(self):
        self.config.pop("deployment")
        manifest = self.module.build_manifest(self.stage, self.config, {"config": {"kind": "files"}})
        self.assertIsNone(manifest["deployment"]["revision"])
        self.assertIn("deployment_revision_unavailable", manifest["limitations"])

    def test_sqlite_counts_are_read_from_export_not_live_source(self):
        (self.stage / "mealie-db").mkdir()
        with sqlite3.connect(self.stage / "mealie-db/database.sqlite") as db:
            db.execute("CREATE TABLE recipes (name TEXT)")
            db.execute("INSERT INTO recipes VALUES ('fixture recipe')")
        self.config["jobs"]["database"]["sources"] = [{"id": "mealie-db", "kind": "sqlite", "path": "/unavailable/live.db"}]
        manifest = self.module.build_manifest(self.stage, self.config, {"mealie-db": {"kind": "sqlite"}})
        self.assertEqual(manifest["sources"]["mealie-db"]["verification"]["baseline"], {"tables": {"recipes": 1}})
        self.assertNotIn("fixture recipe", json.dumps(manifest))

    def test_symlink_artifact_rejected_instead_of_followed(self):
        (self.stage / "config/link").symlink_to("settings.yml")
        with self.assertRaises(self.module.BackupError):
            self.module.build_manifest(self.stage, self.config, {"config": {"kind": "files"}})

if __name__ == "__main__":
    unittest.main()

"""Cross-host recovery must never fall back to another job or implicit versions."""
import importlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "roles/recovery/files"
sys.path.insert(0, str(ROOT / "roles/backup/files"))
sys.path.insert(0, str(FILES))

class RecoveryManifest(unittest.TestCase):
    def setUp(self):
        self.assertTrue((FILES / "recovery_manifest.py").exists(), "missing recovery manifest adapter")
        self.module = importlib.import_module("recovery_manifest")
        self.scope = {"repository": "database", "host": "old.example", "job": "database", "staging_path": "/old/state/staging/database", "snapshot": "latest"}
        self.old = {"id": "a" * 64, "hostname": "old.example", "time": "2026-10-15T09:00:00Z", "tags": ["homelab-config-db:database"], "paths": ["/old/state/staging/database"]}
        self.nodes = [json.dumps({"struct_type": "snapshot", **self.old}), json.dumps({"struct_type": "node", "type": "file", "path": "/manifest.json"})]

    def transport(self, snapshots, nodes=None):
        def run(repo, args, **kwargs):
            if args[0] == "snapshots": return json.dumps(snapshots).encode()
            if args[0] == "ls": return ("\n".join(self.nodes if nodes is None else nodes)).encode()
            raise AssertionError("unexpected repository mutation")
        return patch.object(self.module, "restic", side_effect=run)

    def test_old_host_selected_on_different_target(self):
        other = {**self.old, "id": "b" * 64, "hostname": "new.example"}
        with self.transport([self.old, other]):
            result = self.module.select_snapshot({}, self.scope)
        self.assertEqual(result["snapshot_id"], "a" * 64)
        self.assertEqual(result["host"], "old.example")

    def test_explicit_id_cannot_bypass_completed_scope(self):
        pending = {**self.old, "id": "b" * 64, "tags": ["homelab-pending:database"]}
        self.scope["snapshot"] = "b" * 8
        with self.transport([self.old, pending]), self.assertRaises(self.module.BackupError):
            self.module.select_snapshot({}, self.scope)

    def test_latest_is_pinned_despite_newer_snapshot(self):
        with self.transport([self.old]):
            selected = self.module.select_snapshot({}, self.scope)
        self.scope["snapshot"] = selected["snapshot_id"]
        newer = {**self.old, "id": "c" * 64, "time": "2026-10-15T15:00:00Z"}
        with self.transport([self.old, newer]):
            self.assertEqual(self.module.select_snapshot({}, self.scope)["snapshot_id"], "a" * 64)

    def test_tree_root_is_not_assumed_from_snapshot_paths(self):
        with self.transport([self.old]):
            result = self.module.select_snapshot({}, self.scope)
        self.assertEqual(result["tree_root"], "/")
        self.assertEqual(result["manifest_path"], "/manifest.json")

    def test_two_manifests_are_rejected(self):
        nodes = self.nodes + [json.dumps({"struct_type": "node", "type": "file", "path": "/other/manifest.json"})]
        with self.transport([self.old], nodes), self.assertRaises(self.module.BackupError):
            self.module.select_snapshot({}, self.scope)

    def selection(self):
        return {"snapshot_id": "a" * 64, "host": "old.example", "job": "database", "staging_path": self.scope["staging_path"], "snapshot_time": self.old["time"], "tree_root": "/", "manifest_path": "/manifest.json"}

    def test_v1_is_normalized_without_executing_compose(self):
        raw = {"completed_exports_at": "2026-10-15T09:00:00Z", "sources": {"authentik-db": {"version": "PostgreSQL 16.1", "image": "postgres:16-alpine"}}}
        legacy = {"sources": {"authentik-db": {"kind": "postgres"}}, "images": {"auth": [{"image": "postgres:16-alpine", "registry_digests": ["postgres@sha256:" + "d" * 64], "architecture": "amd64"}]}}
        result = self.module.normalize_manifest(raw, self.selection(), legacy)
        self.assertEqual(result["sources"]["authentik-db"]["artifacts"][0]["relative_path"], "authentik-db/database.dump")
        self.assertIn("legacy_snapshot", result["limitations"])

    def test_unknown_version_and_incomplete_legacy_profile_block_service(self):
        with self.assertRaises(self.module.BackupError):
            self.module.normalize_manifest({"schema_version": 99}, self.selection(), {})
        raw = {"completed_exports_at": "2026-10-15T09:00:00Z", "sources": {"authentik-db": {"version": "PostgreSQL 16.1"}}}
        with self.assertRaises(self.module.BackupError):
            self.module.normalize_manifest(raw, self.selection(), {})

    def test_compose_parser_ignores_commands_and_rejects_object_constructors(self):
        result = self.module.read_compose_fields("services:\n  app:\n    image: app:fixed\n    command: touch /host/marker\n    environment:\n      - SAFE=value\n")
        self.assertEqual(result, {"app": {"image": "app:fixed", "environment": {"SAFE": "value"}}})
        with self.assertRaises(self.module.BackupError):
            self.module.read_compose_fields("!!python/object/apply:os.system ['touch /host/marker']")

    def test_manifest_artifact_cannot_escape_its_source(self):
        raw = {"schema_version": 2, "host": "old.example", "job": "database", "sources": {"config": {"kind": "files", "artifacts": [
            {"relative_path": "../etc/passwd", "entry_type": "file", "size": 1, "sha256": "a" * 64}]}}, "images": {}}
        with self.assertRaises(self.module.BackupError):
            self.module.normalize_manifest(raw, self.selection(), {})

    def test_missing_digest_never_inherits_target_latest(self):
        model = {"images": {"auth": [{"image": "postgres:latest", "registry_digests": [], "architecture": "amd64"}]}, "services": ["auth"]}
        issues = self.module.validate_compatibility(model, {"architecture": "amd64", "services": ["auth"]})
        self.assertEqual(issues[0]["code"], "image_digest_unavailable")

if __name__ == "__main__":
    unittest.main()

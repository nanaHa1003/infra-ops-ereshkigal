"""Restore to approved empty paths only, preserving snapshot configuration."""
import hashlib
import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
import yaml
from jinja2 import Template
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "roles/recovery/files"), str(ROOT / "roles/backup/files")]

class RecoveryFiles(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "roles/recovery/files/recovery_files.py").exists(), "missing recovery file installer")
        self.module = importlib.import_module("recovery_files")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.scratch = self.root / "scratch"
        (self.scratch / "homepage-config/empty").mkdir(parents=True)
        (self.scratch / "homepage-config/settings.yml").write_text("snapshot")
        self.target = {"storage_root": str(self.root / "replacement"), "stack_root": str(self.root / "stacks"),
                       "storage_mount": "/", "scratch_root": str(self.scratch), "services": ["homepage"], "run_id": "run1"}
        self.registry = {"homepage": {"sources": {"homepage-config": {"kind": "files", "target": "homepage/config", "layout": "directory",
                         "owner": os.getuid(), "group": os.getgid(), "mode": "0600"}}}}
        self.model = {"selection": {"tree_root": "/"}, "services": ["homepage"], "sources": {"homepage-config": {"kind": "files", "source_path": "/old/config", "artifacts": [
            {"relative_path": "homepage-config", "entry_type": "directory", "size": 0, "sha256": None},
            {"relative_path": "homepage-config/empty", "entry_type": "directory", "size": 0, "sha256": None},
            {"relative_path": "homepage-config/settings.yml", "entry_type": "file", "size": 8, "sha256": hashlib.sha256(b"snapshot").hexdigest()}]}}}
        self.state = {"run_id": "run1", "owned_paths": []}

    def mappings(self):
        return self.module.build_mappings(self.model, self.target, self.registry)

    def test_new_storage_root_replaces_old_absolute_paths(self):
        mappings = self.mappings()
        self.module.install_artifacts(mappings, self.scratch, self.state)
        result = self.root / "replacement/homepage/config/settings.yml"
        self.assertEqual(result.read_text(), "snapshot")
        self.assertEqual(result.stat().st_mode & 0o777, 0o600)
        self.assertTrue(all("/old/config" not in row["target"] for row in mappings))

    def test_missing_mount_or_nonempty_target_blocks_install(self):
        mappings = self.mappings()
        destination = self.root / "replacement/homepage/config"
        destination.mkdir(parents=True)
        (destination / "existing").write_text("retain")
        with self.assertRaises(self.module.BackupError):
            self.module.validate_targets(mappings, None)
        self.assertEqual((destination / "existing").read_text(), "retain")
        self.target["storage_mount"] = str(self.root / "not-a-mount")
        with self.assertRaises(self.module.BackupError):
            self.module.validate_targets(self.mappings(), None)

    def test_symlink_traversal_overlap_and_scratch_recursion_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        destination = self.root / "replacement"
        destination.mkdir()
        (destination / "homepage").symlink_to(outside)
        with self.assertRaises(self.module.BackupError): self.mappings()
        (destination / "homepage").unlink()
        self.registry["homepage"]["sources"]["homepage-config"]["target"] = "../outside"
        with self.assertRaises(self.module.BackupError): self.mappings()
        self.registry["homepage"]["sources"]["homepage-config"]["target"] = "homepage/config"
        self.target["storage_root"] = str(self.scratch)
        with self.assertRaises(self.module.BackupError): self.mappings()

    def test_same_run_owned_paths_allowed_but_other_run_rejected(self):
        destination = self.root / "replacement/homepage/config"
        destination.mkdir(parents=True)
        (destination / "settings.yml").write_text("old owned copy")
        self.state["owned_paths"] = [str(destination)]
        self.module.validate_targets(self.mappings(), self.state)
        self.state["run_id"] = "another-run"
        with self.assertRaises(self.module.BackupError):
            self.module.validate_targets(self.mappings(), self.state)

    def test_empty_required_directory_is_preserved(self):
        self.module.install_artifacts(self.mappings(), self.scratch, self.state)
        self.assertTrue((self.root / "replacement/homepage/config/empty").is_dir())

    def test_changed_artifact_hash_prevents_copy(self):
        (self.scratch / "homepage-config/settings.yml").write_text("corrupt")
        with self.assertRaises(self.module.BackupError):
            self.module.install_artifacts(self.mappings(), self.scratch, self.state)
        self.assertFalse((self.root / "replacement/homepage/config/settings.yml").exists())

    def test_mealie_prompts_not_overwritten_after_restore(self):
        self.target["services"] = ["mealie"]
        self.registry = {"mealie": {"sources": {"homepage-config": {"kind": "files", "target": "mealie/prompts", "layout": "directory", "owner": os.getuid(), "group": os.getgid(), "mode": "0600"}}}}
        destination = self.root / "replacement/mealie/prompts"
        destination.mkdir(parents=True)
        (destination / "settings.yml").write_text("new repo default")
        self.state["owned_paths"] = [str(destination)]
        self.module.install_artifacts(self.mappings(), self.scratch, self.state)
        self.assertEqual((destination / "settings.yml").read_text(), "snapshot")

    def test_multiple_legacy_directories_copy_all_children(self):
        self.model["schema_version"] = 1
        self.model["sources"]["homepage-config"]["artifacts"] = self.model["sources"]["homepage-config"]["artifacts"][:1]
        (self.scratch / "second-config").mkdir()
        (self.scratch / "second-config/other.yml").write_text("second snapshot")
        self.model["sources"]["second-config"] = {"kind": "files", "artifacts": [{"relative_path": "second-config", "entry_type": "directory", "size": 0, "sha256": None}]}
        self.registry["homepage"]["sources"]["second-config"] = {"kind": "files", "target": "homepage/other", "layout": "directory", "owner": os.getuid(), "group": os.getgid(), "mode": "0600"}
        self.module.install_artifacts(self.mappings(), self.scratch, self.state)
        self.assertTrue((self.root / "replacement/homepage/config/settings.yml").exists(), "legacy directory content was skipped")
        self.assertEqual((self.root / "replacement/homepage/config/settings.yml").read_text(), "snapshot")
        self.assertEqual((self.root / "replacement/homepage/other/other.yml").read_text(), "second snapshot")

    def test_registry_covers_current_backup_sources_with_static_paths(self):
        path = ROOT / "roles/recovery/vars/services.yml"
        self.assertTrue(path.exists(), "missing service destination registry")
        registry = yaml.safe_load(path.read_text())["recovery_service_registry"]
        host = yaml.safe_load((ROOT / "inventory/host_vars/ereshkigal.netbird.cloud/vars.yml").read_text())
        definitions = {identifier: definition for service in registry.values() for identifier, definition in service["sources"].items()}
        self.assertEqual(set(definitions), {item["id"] for item in host["backup_jobs"]["database"]["sources"]})
        for source in host["backup_jobs"]["database"]["sources"]:
            definition = definitions[source["id"]]
            self.assertEqual(definition["kind"], source["kind"])
            if definition["layout"] != "reference" and "path" in source:
                original = Template(source["path"]).render(storage_root="/mnt/storage")
                self.assertEqual("/mnt/storage/" + definition["target"], original)

    def test_legacy_nested_symlink_fails_before_creating_target(self):
        self.model["schema_version"] = 1
        self.model["sources"]["homepage-config"]["artifacts"] = self.model["sources"]["homepage-config"]["artifacts"][:1]
        (self.scratch / "homepage-config/empty/link").symlink_to(self.root / "outside")
        with self.assertRaises(self.module.BackupError):
            self.module.install_artifacts(self.mappings(), self.scratch, self.state)
        self.assertFalse((self.root / "replacement/homepage/config").exists(), "target created before source validation finished")

    @unittest.skipIf(os.geteuid() == 0, "non-root ownership boundary")
    def test_other_container_uid_requires_root_instead_of_claiming_ownership(self):
        self.registry["homepage"]["sources"]["homepage-config"]["owner"] = os.getuid() + 1
        with self.assertRaisesRegex(self.module.BackupError, "root required"):
            self.module.install_artifacts(self.mappings(), self.scratch, self.state)
        self.assertFalse((self.root / "replacement/homepage/config/settings.yml").exists())

    def test_wordpress_core_precedes_restored_custom_files(self):
        self.target.update(services=["wordpress"], wordpress_core_image="wordpress@sha256:" + "a" * 64)
        self.registry = {"wordpress": {"sources": {"homepage-config": {"kind": "files", "target": "wordpress/html", "layout": "directory", "owner": os.getuid(), "group": os.getgid(), "mode": "0600"}}}}
        def core(image, destination, state):
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "index.php").write_text("core")
            (destination / "settings.yml").write_text("image default")
        with patch.object(self.module, "prepare_wordpress_core", side_effect=core):
            self.module.install_artifacts(self.mappings(), self.scratch, self.state)
        self.assertEqual((self.root / "replacement/wordpress/html/index.php").read_text(), "core")
        self.assertEqual((self.root / "replacement/wordpress/html/settings.yml").read_text(), "snapshot")

if __name__ == "__main__": unittest.main()

class WordPressCreationIntent(unittest.TestCase):
    def test_core_container_intent_is_persisted_before_create(self):
        import recovery_files as module
        import recovery_state
        import json
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve()
            state=recovery_state.open_run({'run_id':'fixture-core','target':{'machine_id':'fixture'},'selection':{'snapshot_id':'a'*64},'profile_sha256':'b'*64,'services':['wordpress'],'approved_roots':[]},root/'state')
            def create(args,**kwargs):
                disk=json.loads(Path(state['_state_file']).read_text())
                self.assertIn('recovery-core-fixture-core',disk['owned_containers'])
                raise module.BackupError('fixture creation failure')
            with patch.object(module,'command',side_effect=create),self.assertRaises(module.BackupError):
                module.prepare_wordpress_core('wordpress@sha256:'+'a'*64,root/'html',state)

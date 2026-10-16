"""Render the deployed configuration and exercise its validation contract."""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from jinja2 import Environment, StrictUndefined
import yaml

ROOT = Path(__file__).resolve().parents[1]


class BackupConfiguration(unittest.TestCase):
    def render_config(self):
        template = ROOT / "roles/backup/templates/config.json.j2"
        self.assertTrue(template.exists(), "root-only backup configuration template is missing")
        self.values = yaml.safe_load((ROOT / "roles/backup/defaults/main.yml").read_text())
        self.values.update(yaml.safe_load((ROOT / "inventory/host_vars/ereshkigal.netbird.cloud/vars.yml").read_text()))
        self.values.update(yaml.safe_load((ROOT / "inventory/group_vars/all/vault.ci.yml").read_text()))
        self.values.update(storage_root="/mnt/storage", stack_root="/opt/stack", inventory_hostname="ereshkigal.netbird.cloud")
        self.values["vault_secrets"]["backup"]["repositories"]["database"]["restic_password"] = 'quotes" and\nnewline'
        env = Environment(undefined=StrictUndefined)
        env.filters["to_json"] = json.dumps
        env.filters["combine"] = lambda first, second: {**first, **second}
        # Ansible recursively templates inventory strings; reproduce that boundary.
        def expand(value):
            if isinstance(value, dict): return {key: expand(item) for key, item in value.items()}
            if isinstance(value, list): return [expand(item) for item in value]
            return env.from_string(value).render(self.values) if isinstance(value, str) else value
        self.values = expand(self.values)
        return json.loads(env.from_string(template.read_text()).render(self.values))

    def test_production_config_preserves_prefix_secrets_and_source_allowlist(self):
        config = self.render_config()
        repo = config["repositories"]["database"]
        self.assertEqual(repo["url"], "s3:https://s3.us-west-004.backblazeb2.com/homelab-db-snapshots/ereshkigal")
        self.assertEqual(repo["restic_password"], 'quotes" and\nnewline')
        job = config["jobs"]["database"]
        self.assertEqual(job["repository"], "database")
        paths = [source.get("path", "") for source in job["sources"]]
        self.assertIn("/mnt/storage/n8n/data/config", paths)
        self.assertIn("/mnt/storage/pinchflat/config/db/pinchflat.db", paths)
        self.assertNotIn("/mnt/storage", paths)
        self.assertNotIn("/mnt/storage/immich/library", paths)
        defaults = yaml.safe_load((ROOT / "roles/backup/defaults/main.yml").read_text())
        self.assertFalse(defaults["backup_timers_enabled"])
        self.assertFalse(self.values["backup_initialize_repositories"])
        sys.path.insert(0, str(ROOT / "roles/backup/files"))
        config["state_root"] = str(Path(tempfile.gettempdir()).resolve() / "homelab-backup-test")
        importlib.import_module("backup_runner").validate_config(config)

    def test_packaging_protects_secrets_without_implicitly_starting_backups(self):
        self.render_config()
        tasks = yaml.safe_load((ROOT / "roles/backup/tasks/main.yml").read_text())
        secret = next(task for task in tasks if task.get("ansible.builtin.template", {}).get("src") == "config.json.j2")
        self.assertEqual(secret["ansible.builtin.template"]["mode"], "0600")
        self.assertTrue(secret["no_log"])
        self.assertFalse(secret["diff"])
        for task in tasks:
            if "ansible.builtin.command" in task:
                self.assertNotIn(" init ", str(task["ansible.builtin.command"]))
                self.assertNotIn(" backup ", str(task["ansible.builtin.command"]))
        play = yaml.safe_load((ROOT / "backup.yml").read_text())[0]
        self.assertEqual(play["roles"], ["backup"])


if __name__ == "__main__":
    unittest.main()

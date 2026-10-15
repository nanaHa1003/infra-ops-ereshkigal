"""State protects a fixed target/snapshot and never guesses ownership."""
from copy import deepcopy
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "roles/recovery/files"), str(ROOT / "roles/backup/files")]

class RecoveryState(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "roles/recovery/files/recovery_state.py").exists(), "missing recovery state module")
        self.module = importlib.import_module("recovery_state")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.plan = {"run_id": "fixture-run", "selection": {"snapshot_id": "a" * 64}, "profile_sha256": "b" * 64,
                     "target": {"machine_id": "target-one"}, "services": ["auth"], "approved_roots": [str(self.root / "new/auth") ]}
        self.state_root = self.root / "recovery"

    def test_profile_snapshot_or_machine_change_rejects_resume(self):
        state = self.module.open_run(self.plan, self.state_root)
        self.assertEqual(state["target_machine_id"], "target-one")
        self.assertEqual((self.state_root / "runs/fixture-run").stat().st_mode & 0o777, 0o700)
        path = self.state_root / "runs/fixture-run/state.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for key in ("profile_sha256", "selection", "target"):
            other = deepcopy(self.plan)
            other[key] = {"snapshot_id": "c" * 64} if key == "selection" else {"machine_id": "different-machine"} if key == "target" else "c" * 64
            with self.subTest(key=key), self.assertRaises(self.module.BackupError):
                self.module.open_run(other, self.state_root)

    def test_atomic_state_survives_interruption(self):
        state = self.module.open_run(self.plan, self.state_root)
        self.module.advance_service(state, "auth", "downloaded", {})
        with patch.object(self.module.os, "replace", side_effect=OSError("fixture interruption")):
            with self.assertRaises(self.module.BackupError):
                self.module.advance_service(state, "auth", "prepared", {})
        disk = json.loads((self.state_root / "runs/fixture-run/state.json").read_text())
        self.assertEqual(disk["services"]["auth"]["phase"], "downloaded")

    def test_cleanup_refuses_unknown_paths_or_active_production_containers(self):
        state = self.module.open_run(self.plan, self.state_root)
        state["owned_paths"] = ["/etc"]
        with self.assertRaises(self.module.BackupError): self.module.cleanup_preview(state)
        state["owned_paths"] = []
        state["owned_containers"] = ["auth-postgresql-1"]
        with self.assertRaises(self.module.BackupError): self.module.cleanup_preview(state)
        state["owned_containers"] = []
        state["activation"]["services_started"] = ["auth"]
        with self.assertRaises(self.module.BackupError): self.module.cleanup_preview(state)

    def test_cleanup_stops_owned_resources_before_removing_data(self):
        state = self.module.open_run(self.plan, self.state_root)
        state['owned_paths'] = self.plan['approved_roots']
        state['owned_containers'] = ['recovery-db-fixture-run-auth']
        state['owned_networks'] = ['recovery-fixture-run']
        self.assertEqual([item['kind'] for item in self.module.cleanup_preview(state)], ['container', 'network', 'path'])

    def test_run_lock_rejects_overlapping_restore(self):
        with self.module.run_lock(self.state_root):
            with self.assertRaises(self.module.BackupError):
                with self.module.run_lock(self.state_root): pass

    def test_service_state_cannot_move_backwards(self):
        state = self.module.open_run(self.plan, self.state_root)
        for phase in ("downloaded", "prepared", "imported"):
            self.module.advance_service(state, "auth", phase, {})
        with self.assertRaises(self.module.BackupError):
            self.module.advance_service(state, "auth", "prepared", {})

if __name__ == "__main__": unittest.main()

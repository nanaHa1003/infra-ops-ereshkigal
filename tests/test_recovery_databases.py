"""Data import is bounded to run-owned volumes, matching engines and real roles."""
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "roles/recovery/files"), str(ROOT / "roles/backup/files")]

class RecoveryDatabases(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "roles/recovery/files/recovery_databases.py").exists(), "missing database restore adapter")
        self.module = importlib.import_module("recovery_databases")
        self.state_module = importlib.import_module("recovery_state")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        plan = {"run_id": "fixture-run", "selection": {"snapshot_id": "a" * 64}, "profile_sha256": "b" * 64,
            "target": {"machine_id": "fixture-machine"}, "services": ["auth"], "approved_roots": [str(self.root / "data/authentik/postgresql")]}
        self.state = self.state_module.open_run(plan, self.root / "state")
        for phase in ("downloaded", "prepared"): self.state_module.advance_service(self.state, "auth", phase, {})
        self.scratch = Path(self.state["_run_root"]) / "scratch"
        (self.scratch / "authentik-db").mkdir(parents=True)
        self.dump = self.scratch / "authentik-db/database.dump"
        self.dump.write_bytes(b"PGDMP-fixture")
        self.model = {"selection": {"tree_root": "/"}, "services": ["auth"], "sources": {"authentik-db": {"kind": "postgres", "version": "PostgreSQL 16.1", "role": "authentik", "database": "authentik", "extensions": [],
            "artifacts": [{"relative_path": "authentik-db/database.dump", "entry_type": "file", "size": 13, "sha256": None}]}}}
        self.target = {"storage_root": str(self.root / "data"), "stack_root": str(self.root / "stack"), "storage_mount": "/", "scratch_root": str(self.scratch), "services": ["auth"], "run_id": "fixture-run",
            "secrets": {"authentik": {"pgpass": "fixture-password"}}, "image_variables": {"auth_postgres_image": "postgres@sha256:" + "a" * 64},
            "registry": {"auth": {"sources": {"authentik-db": {"kind": "postgres", "target": "authentik/postgresql", "layout": "database", "owner": os.getuid(), "group": os.getgid(), "mode": "0600"}}}}}

    def test_interrupted_import_does_not_reimport_automatically(self):
        with patch.object(self.module, "running_volume_users", return_value=[]), patch.object(self.module, "_restore_postgres", side_effect=self.module.BackupError("fixture import failure")):
            with self.assertRaises(self.module.BackupError):
                self.module.restore_database("auth", self.model, self.target, self.state)
        with patch.object(self.module, "running_volume_users", return_value=[]), patch.object(self.module, "_restore_postgres", side_effect=AssertionError("must not rerun failed import")):
            with self.assertRaises(self.module.BackupError):
                self.module.restore_database("auth", self.model, self.target, self.state)
        disk = json.loads(Path(self.state["_state_file"]).read_text())
        self.assertEqual(disk["services"]["auth"]["database_import"], "failed")

    def test_completed_import_is_not_loaded_twice(self):
        with patch.object(self.module, "running_volume_users", return_value=[]), patch.object(self.module, "_restore_postgres", return_value={"engine_version": "16.1"}):
            first = self.module.restore_database("auth", self.model, self.target, self.state)
        with patch.object(self.module, "running_volume_users", return_value=[]), patch.object(self.module, "_restore_postgres", side_effect=AssertionError("duplicate import")):
            second = self.module.restore_database("auth", self.model, self.target, self.state)
        self.assertEqual(first, second)
        self.assertEqual(self.state["services"]["auth"]["phase"], "imported")

    def test_empty_dump_fails_before_container_creation(self):
        self.dump.write_bytes(b"")
        with patch.object(self.module, "_docker", side_effect=AssertionError("must not create DB for empty dump")):
            with self.assertRaises(self.module.BackupError):
                self.module.restore_database("auth", self.model, self.target, self.state)

    def test_database_volume_has_only_one_running_owner(self):
        with patch.object(self.module, "running_volume_users", return_value=["unrelated-database"]):
            with self.assertRaises(self.module.BackupError):
                self.module.restore_database("auth", self.model, self.target, self.state)
        self.assertEqual(self.state["services"]["auth"]["phase"], "prepared")

    def test_pg18_uses_parent_mount_without_exposing_ports_or_passwords(self):
        source = self.model["sources"]["authentik-db"]
        source["version"] = "PostgreSQL 18.1"
        spec = self.module.database_spec("auth", self.model, self.target, self.state)
        with patch.object(self.module.os, "chown"), patch.object(self.module, "_docker", return_value=b"70") as boundary:
            self.module.create_database_container(spec, self.state)
        args = boundary.call_args.args[0]
        self.assertTrue(any(":/var/lib/postgresql:Z" in item for item in args))
        self.assertNotIn("--publish", args)
        self.assertNotIn("-p", args)
        self.assertNotIn("--privileged", args)
        self.assertNotIn("fixture-password", " ".join(args))

    def test_postgres_app_role_is_not_bootstrap_superuser(self):
        spec = self.module.database_spec("auth", self.model, self.target, self.state)
        sql = self.module.postgres_setup_sql(spec)
        self.assertIn('CREATE ROLE "authentik" LOGIN NOSUPERUSER', sql)
        self.assertNotEqual(spec["app_user"], spec["admin_user"])
        self.assertIn('NOLOGIN SUPERUSER', sql)
        self.assertIn('OWNER "authentik"', sql)

    def test_immich_preloads_are_restricted_to_recorded_supported_libraries(self):
        source = self.model["sources"]["authentik-db"]
        source.update(version="PostgreSQL 14.1", preload_libraries="vchord.so,vectors.so")
        spec = self.module.database_spec("auth", self.model, self.target, self.state)
        self.assertEqual(spec["preload_libraries"], ["vchord.so", "vectors.so"])
        source["preload_libraries"] = "../../unknown.so"
        with self.assertRaises(self.module.BackupError): self.module.database_spec("auth", self.model, self.target, self.state)

    def test_ready_timeout_is_sanitized(self):
        spec = self.module.database_spec("auth", self.model, self.target, self.state)
        with patch.object(self.module, "_docker", side_effect=self.module.BackupError("fixture stderr")), patch.object(self.module.time, "monotonic", side_effect=[0, 121]):
            with self.assertRaisesRegex(self.module.BackupError, "database readiness timed out"):
                self.module.wait_database(spec)

if __name__ == "__main__": unittest.main()

class AdditionalDatabaseChecks(unittest.TestCase):
    setUp=RecoveryDatabases.setUp
    def test_postgres_named_database_uses_alter_owner_to(self):
        spec = {'app_user': 'app', 'database': 'postgres', 'import_role': 'temporary', 'app_password': 'fake'}
        self.assertIn('ALTER DATABASE "postgres" OWNER TO "app";', self.module.postgres_setup_sql(spec))

    def test_sqlite_rejects_leftover_wal(self):
        path = self.root / 'test.sqlite'
        with sqlite3.connect(path) as db: db.execute('CREATE TABLE test(id INTEGER)')
        Path(str(path) + '-wal').write_bytes(b'stale')
        with self.assertRaises(self.module.BackupError): self.module._restore_sqlite({'target_file': str(path)}, self.state)

    def test_sqlite_checks_actual_integrity(self):
        path = self.root / 'test.sqlite'
        with sqlite3.connect(path) as db: db.execute('CREATE TABLE test(id INTEGER)')
        self.assertTrue(self.module._restore_sqlite({'target_file': str(path)}, self.state)['sqlite_integrity'])
        path.write_bytes(b'corrupt SQLite')
        with self.assertRaises(self.module.BackupError): self.module._restore_sqlite({'target_file': str(path)}, self.state)

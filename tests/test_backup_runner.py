"""Run the real runner against controlled process boundaries; never B2."""
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

FILES = Path(__file__).resolve().parents[1] / "roles/backup/files"


class BackupRunner(unittest.TestCase):
    def setUp(self):
        self.assertTrue((FILES / "backup_runner.py").exists(), "backup runner is missing")
        sys.path.insert(0, str(FILES))
        self.runner = importlib.import_module("backup_runner")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.jsonl"
        executable = self.bin / "restic"
        executable.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['BACKUP_TEST_LOG'], 'a') as output:
    output.write(json.dumps(args) + '\\n')
if 'backup' in args:
    pathlib.Path(os.environ['BACKUP_TEST_SNAPSHOT']).write_text(json.dumps(sorted(str(p.relative_to('.')) for p in pathlib.Path('.').rglob('*') if p.is_file())))
    print(json.dumps({'message_type': 'summary', 'snapshot_id': 'a' * 64}))
if 'snapshots' in args:
    sys.exit(int(os.environ.get('BACKUP_TEST_PROBE_EXIT', '0')))
if os.environ.get('BACKUP_TEST_STDERR'):
    print(os.environ['BACKUP_TEST_STDERR'], file=sys.stderr)
sys.exit(int(os.environ.get('BACKUP_TEST_EXIT', '0')))
''')
        executable.chmod(0o700)
        self.env = patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                                          "BACKUP_TEST_LOG": str(self.log),
                                          "BACKUP_TEST_SNAPSHOT": str(self.root / "snapshot.json")})
        self.env.start()
        self.addCleanup(self.env.stop)
        source = self.root / "settings.yml"
        source.write_text("good")
        self.config = {"state_root": str(self.root / "state"), "host": "test-host", "repositories": {
            "database": {"url": str(self.root / "repo"), "access_key_id": "FAKE-KEY",
                         "secret_access_key": "FAKE-SECRET", "restic_password": "FAKE-PASSWORD"}},
            "jobs": {"database": {"repository": "database", "healthchecks_ping_url": "",
                "sources": [{"id": "settings", "kind": "files", "path": str(source)}],
                "retention": {"within": "48h", "daily": 14, "weekly": 8, "monthly": 12}}}}

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_completed_batch_uploaded_then_staging_removed(self):
        self.assertEqual(self.runner.run_job(self.config, "database"), 0)
        self.assertIn("settings/settings.yml", json.loads((self.root / "snapshot.json").read_text()))
        self.assertFalse((self.root / "state/staging/database").exists())
        backup = self.calls()[0]
        self.assertIn("--host", backup)
        self.assertIn("test-host", backup)

    def test_failed_export_never_uploads_stale_staging(self):
        stale = self.root / "state/staging/database/old.sql"
        stale.parent.mkdir(parents=True)
        stale.write_text("stale data")
        self.config["jobs"]["database"]["sources"][0]["path"] = str(self.root / "missing")
        self.assertNotEqual(self.runner.run_job(self.config, "database"), 0)
        self.assertEqual(self.calls(), [])
        self.assertFalse(stale.exists())

    def test_partial_restic_exit_is_failure_and_does_not_leak_secrets(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"BACKUP_TEST_EXIT": "3"}), contextlib.redirect_stderr(output):
            self.assertNotEqual(self.runner.run_job(self.config, "database"), 0)
        self.assertNotIn("FAKE-SECRET", output.getvalue())
        self.assertNotIn("FAKE-PASSWORD", output.getvalue())
        self.assertIn("homelab-pending:database", self.calls()[0])
        self.assertNotIn("homelab-config-db:database", self.calls()[0])
        self.assertFalse(any("tag" == args[0] for args in self.calls()))

    def test_unknown_repository_fails_before_commands(self):
        self.config["jobs"]["database"]["repository"] = "missing"
        self.assertNotEqual(self.runner.run_job(self.config, "database"), 0)
        self.assertEqual(self.calls(), [])

    def test_ids_cannot_escape_state_directory(self):
        self.config["jobs"]["../escape"] = self.config["jobs"].pop("database")
        self.assertNotEqual(self.runner.run_job(self.config, "../escape"), 0)
        self.assertEqual(self.calls(), [])

    def test_retention_preview_is_scoped_and_never_prunes(self):
        self.assertEqual(self.runner.maintenance(self.config, "database", preview=True), 0)
        args = self.calls()[0]
        for flag, value in (("--keep-within", "48h"), ("--keep-daily", "14"),
                            ("--keep-weekly", "8"), ("--keep-monthly", "12"), ("--host", "test-host")):
            self.assertEqual(args[args.index(flag) + 1], value)
        self.assertIn("--dry-run", args)
        self.assertIn("--tag", args)
        self.assertNotIn("--prune", args)

    def test_failed_upload_never_initializes_repository(self):
        with patch.dict(os.environ, {"BACKUP_TEST_EXIT": "1"}):
            self.assertNotEqual(self.runner.run_job(self.config, "database"), 0)
        self.assertFalse(any("init" in args for args in self.calls()))

    def test_cleanup_holds_the_repository_lock(self):
        original = self.runner.shutil.rmtree
        lock_was_held = []
        def observe(path):
            try:
                with self.runner.repository_lock(self.config):
                    lock_was_held.append(False)
            except self.runner.BackupError:
                lock_was_held.append(True)
            original(path)
        with patch.object(self.runner.shutil, "rmtree", side_effect=observe):
            self.assertEqual(self.runner.run_job(self.config, "database"), 0)
        self.assertEqual(lock_was_held, [True])

    def test_overlapping_job_does_not_upload_or_remove_active_staging(self):
        with self.runner.repository_lock(self.config):
            active = self.root / "state/staging/database/active"
            active.parent.mkdir(parents=True)
            active.write_text("in progress")
            self.assertNotEqual(self.runner.run_job(self.config, "database"), 0)
            self.assertTrue(active.exists())
        self.assertEqual(self.calls(), [])

    def test_monitoring_receives_start_success_and_failure_without_contents(self):
        self.config["jobs"]["database"]["healthchecks_ping_url"] = "https://hc-ping.com/00000000-0000-0000-0000-000000000000"
        requests = []
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return False
        def transport(request, **kwargs):
            requests.append((request.full_url.rsplit("/", 1)[-1], request.method, request.data))
            return Response()
        with patch.object(self.runner.urllib.request, "urlopen", side_effect=transport):
            self.assertEqual(self.runner.run_job(self.config, "database"), 0)
            with patch.dict(os.environ, {"BACKUP_TEST_EXIT": "1"}):
                self.assertNotEqual(self.runner.run_job(self.config, "database"), 0)
        self.assertEqual([request[0] for request in requests], ["start", "00000000-0000-0000-0000-000000000000", "start", "fail"])
        self.assertTrue(all(method == "POST" and body == b"" for _, method, body in requests))

    def test_monitoring_outage_preserves_the_uploaded_snapshot(self):
        self.config["jobs"]["database"]["healthchecks_ping_url"] = "https://hc-ping.com/00000000-0000-0000-0000-000000000000"
        with patch.object(self.runner.urllib.request, "urlopen", side_effect=OSError("FAKE-SECRET")):
            output = io.StringIO()
            with contextlib.redirect_stderr(output):
                self.assertEqual(self.runner.run_job(self.config, "database"), 2)
        self.assertEqual(len(self.calls()), 2)
        self.assertTrue((self.root / "snapshot.json").exists())
        self.assertNotIn("FAKE-SECRET", output.getvalue())

    def test_failed_data_check_does_not_advance_rotation(self):
        with patch.dict(os.environ, {"BACKUP_TEST_EXIT": "1"}):
            self.assertNotEqual(self.runner.check_data(self.config, "database"), 0)
        self.assertFalse((self.root / "state/database-check-quarter").exists())
        self.assertEqual(self.runner.check_data(self.config, "database"), 0)
        self.assertEqual((self.root / "state/database-check-quarter").read_text(), "2")
        self.assertEqual(self.calls()[-1], ["check", "--read-data-subset", "1/4"])

    def test_manual_full_check_does_not_change_rotation(self):
        self.assertEqual(self.runner.check_data(self.config, "database", full=True), 0)
        self.assertEqual(self.calls(), [["check", "--read-data"]])
        self.assertFalse((self.root / "state/database-check-quarter").exists())

    def test_access_denied_error_is_actionable_without_exposing_raw_stderr(self):
        with patch.dict(os.environ, {"BACKUP_TEST_EXIT": "1", "BACKUP_TEST_STDERR": "Access Denied FAKE-SECRET FAKE-PASSWORD"}):
            with self.assertRaises(self.runner.BackupError) as raised:
                self.runner.restic(self.config["repositories"]["database"], ["init"])
        self.assertIn("AccessDenied", str(raised.exception))
        self.assertNotIn("FAKE-SECRET", str(raised.exception))
        self.assertNotIn("FAKE-PASSWORD", str(raised.exception))

    def test_ensure_init_does_not_reinitialize_accessible_repository(self):
        self.assertFalse(self.runner.ensure_initialized(self.config, "database"))
        self.assertEqual(self.calls(), [["snapshots", "--json"]])

    def test_ensure_init_only_initializes_an_absent_repository(self):
        with patch.dict(os.environ, {"BACKUP_TEST_PROBE_EXIT": "10"}):
            self.assertTrue(self.runner.ensure_initialized(self.config, "database"))
        self.assertEqual(self.calls(), [["snapshots", "--json"], ["init"]])

    def test_ensure_init_does_not_initialize_on_access_or_password_failure(self):
        for code in (1, 12):
            self.log.unlink(missing_ok=True)
            with patch.dict(os.environ, {"BACKUP_TEST_PROBE_EXIT": str(code)}):
                with self.assertRaises(self.runner.BackupError):
                    self.runner.ensure_initialized(self.config, "database")
            self.assertEqual(self.calls(), [["snapshots", "--json"]])

    def test_failed_upload_reports_phase_and_exit_without_raw_errors(self):
        output=io.StringIO()
        with patch.dict(os.environ,{'BACKUP_TEST_EXIT':'3','BACKUP_TEST_STDERR':'FAKE-SECRET FAKE-PASSWORD'}), contextlib.redirect_stderr(output):
            self.assertNotEqual(self.runner.run_job(self.config,'database'),0)
        self.assertIn('phase=upload',output.getvalue())
        self.assertIn('restic exit=3',output.getvalue())
        self.assertNotIn('FAKE-SECRET',output.getvalue())
        self.assertNotIn('FAKE-PASSWORD',output.getvalue())

    def test_unexpected_exception_reports_phase_without_exception_text(self):
        output=io.StringIO()
        with patch.object(self.runner,'prepare_sources',side_effect=ValueError('FAKE-SECRET')),contextlib.redirect_stderr(output):
            self.assertNotEqual(self.runner.run_job(self.config,'database'),0)
        self.assertIn('phase=source-export',output.getvalue())
        self.assertNotIn('FAKE-SECRET',output.getvalue())


if __name__ == "__main__":
    unittest.main()

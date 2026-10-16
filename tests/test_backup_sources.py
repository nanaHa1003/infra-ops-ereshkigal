"""Exercise consistent exports; in particular a live WAL must not be omitted."""
import importlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

FILES = Path(__file__).resolve().parents[1] / "roles/backup/files"


class BackupSources(unittest.TestCase):
    def setUp(self):
        self.assertTrue((FILES / "backup_sources.py").exists(), "backup source adapters are missing")
        sys.path.insert(0, str(FILES))
        self.sources = importlib.import_module("backup_sources")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.output = self.root / "export"

    def test_sqlite_export_includes_uncheckpointed_wal(self):
        path = self.root / "live.db"
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE recipes (name TEXT)")
            db.execute("INSERT INTO recipes VALUES ('soup')")
            db.commit()
            self.sources.prepare_sources([{"id": "mealie", "kind": "sqlite", "path": str(path)}], self.output)
            with sqlite3.connect(self.output / "mealie/database.sqlite") as restored:
                self.assertEqual(restored.execute("SELECT name FROM recipes").fetchall(), [("soup",)])
        self.assertEqual(list(self.output.rglob("*-wal")), [])

    def test_missing_required_source_fails_without_creating_a_database(self):
        path = self.root / "absent.db"
        with self.assertRaises(self.sources.BackupError):
            self.sources.prepare_sources([{"id": "missing", "kind": "sqlite", "path": str(path)}], self.output)
        self.assertFalse(path.exists())

    def test_file_allowlist_excludes_uploads_and_live_databases(self):
        source = self.root / "config"
        source.mkdir()
        (source / "settings.yml").write_text("configured")
        (source / "uploads").mkdir()
        (source / "uploads/photo.jpg").write_bytes(b"media")
        (source / "live.db").write_bytes(b"not-safe")
        self.sources.prepare_sources([{"id": "config", "kind": "files", "path": str(source),
                                      "exclude": ["uploads", "*.db"]}], self.output)
        self.assertEqual((self.output / "config/settings.yml").read_text(), "configured")
        self.assertFalse((self.output / "config/uploads").exists())
        self.assertFalse((self.output / "config/live.db").exists())
        self.assertEqual((self.output / "config/settings.yml").stat().st_mode & 0o777, 0o600)

    def test_symlink_escape_is_rejected(self):
        source = self.root / "config"
        source.mkdir()
        (source / "escape").symlink_to(self.root / "secret")
        with self.assertRaises(self.sources.BackupError):
            self.sources.prepare_sources([{"id": "config", "kind": "files", "path": str(source)}], self.output)

    def test_unreadable_config_directory_fails_instead_of_silently_skipping(self):
        source = self.root / "config"
        source.mkdir()
        with patch.object(self.sources.os, "scandir", side_effect=PermissionError("secret path")):
            with self.assertRaises(self.sources.BackupError):
                self.sources.prepare_sources([{"id": "config", "kind": "files", "path": str(source)}], self.output)

    def test_source_id_cannot_escape_staging(self):
        with self.assertRaises(self.sources.BackupError):
            self.sources.prepare_sources([{"id": "../escape", "kind": "files", "path": str(self.root)}], self.output)

    def fake_docker(self):
        binary = self.root / "docker"
        binary.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ['DOCKER_TEST_LOG'], 'a') as output:
    output.write(json.dumps(args) + '\\n')
script = args[-1]
if args[0] == 'inspect': print('postgres:test')
elif 'pg_dump ' in script:
    if os.environ.get('DOCKER_TEST_FAILURE'): sys.exit(1)
    sys.stdout.buffer.write(b'PGDMP-test-archive')
elif 'json_build_object' in script:
    print(json.dumps({'version': 'PostgreSQL test', 'extensions': [{'name': 'vector', 'version': 'test'}]}))
elif 'JSON_OBJECT' in script: print(json.dumps({'version':'11.4.5','database':'wordpress','role':'wordpress','definers':['wordpress@%']}))
elif 'information_schema' in script: print(os.environ.get('DOCKER_TEST_ENGINE', 'InnoDB'))
elif '--list' in args:
    assert sys.stdin.buffer.read() == b'PGDMP-test-archive'
elif 'mariadb-dump ' in script: print('SQL fixture')
elif '--version' in args: print('MariaDB test')
else: sys.exit(99)
''')
        binary.chmod(0o700)
        return patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                                       "DOCKER_TEST_LOG": str(self.root / "docker-calls")})

    def test_postgres_export_checks_archive_and_records_extensions(self):
        with self.fake_docker():
            metadata = self.sources.prepare_sources([{"id": "pg", "kind": "postgres", "container": "pg-test"}], self.output)
        self.assertEqual((self.output / "pg/database.dump").read_bytes(), b"PGDMP-test-archive")
        self.assertEqual(metadata["pg"]["extensions"], [{"name": "vector", "version": "test"}])

    def test_failed_postgres_dump_rejects_batch(self):
        with self.fake_docker(), patch.dict(os.environ, {"DOCKER_TEST_FAILURE": "1"}):
            with self.assertRaises(self.sources.BackupError):
                self.sources.prepare_sources([{"id": "pg", "kind": "postgres", "container": "pg-test"}], self.output)

    def test_mariadb_nontransactional_tables_rejected_before_dump(self):
        with self.fake_docker(), patch.dict(os.environ, {"DOCKER_TEST_ENGINE": "MyISAM"}):
            with self.assertRaises(self.sources.BackupError):
                self.sources.prepare_sources([{"id": "wp", "kind": "mariadb", "container": "wp-db"}], self.output)
        self.assertFalse((self.output / "wp/database.sql").exists())

    def test_mariadb_export_includes_schema_objects_and_transaction_flags(self):
        with self.fake_docker():
            metadata = self.sources.prepare_sources([{"id": "wp", "kind": "mariadb", "container": "wp-db"}], self.output)
        self.assertEqual(metadata["wp"]["database"], "wordpress")
        self.assertEqual(metadata["wp"]["definers"], ["wordpress@%"] )
        self.assertEqual((self.output / "wp/database.sql").read_text(), "SQL fixture\n")
        calls = [json.loads(line) for line in (self.root / "docker-calls").read_text().splitlines()]
        script = next(call[-1] for call in calls if "mariadb-dump " in call[-1])
        for flag in ("--single-transaction", "--routines", "--events", "--triggers"):
            self.assertIn(flag, script)


if __name__ == "__main__":
    unittest.main()

"""Only report field names and booleans for historical Vault comparisons."""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "roles/recovery/files"), str(ROOT / "roles/backup/files")]

class RecoverySecrets(unittest.TestCase):
    def test_historical_vault_mismatch_reports_no_values(self):
        self.assertTrue((ROOT / "roles/recovery/files/recovery_files.py").exists(), "missing recovery secret comparison")
        module = importlib.import_module("recovery_files")
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory).resolve()
            (scratch / "auth-compose").mkdir()
            (scratch / "auth-compose/docker-compose.yml").write_text('services:\n  postgresql:\n    image: postgres:16-alpine\n    environment:\n      POSTGRES_PASSWORD: fixture-old-password\n  authentik-server:\n    image: authentik:fixed\n    environment:\n      AUTHENTIK_SECRET_KEY: fixture-old-key\n')
            model = {"selection": {"tree_root": "/"}, "services": ["auth"], "sources": {"auth-compose": {}}}
            result = module.compare_recovery_secrets(model, scratch, {"authentik": {"pgpass": "fixture-old-password", "secret_key": "fixture-new-key"}})
            self.assertTrue(next(row for row in result if row["field"] == "authentik.pgpass")["matched"])
            self.assertFalse(next(row for row in result if row["field"] == "authentik.secret_key")["matched"])
            encoded = json.dumps(result)
            self.assertNotIn("fixture-old-password", encoded)
            self.assertNotIn("fixture-old-key", encoded)
            self.assertNotIn("fixture-new-key", encoded)

if __name__ == "__main__": unittest.main()

class DatabaseIdentity(unittest.TestCase):
    def test_same_password_does_not_hide_changed_wordpress_database_identity(self):
        module=importlib.import_module('recovery_files')
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();(root/'wordpress-compose').mkdir()
            (root/'wordpress-compose/docker-compose.yml').write_text('services:\n  mariadb:\n    image: mariadb:fixed\n    environment:\n      MYSQL_PASSWORD: same-password\n      MYSQL_USER: old-user\n      MYSQL_DATABASE: old-database\n')
            model={'selection':{'tree_root':'/'},'services':['wordpress']}
            result=module.compare_recovery_secrets(model,root,{'wordpress':{'mysql_password':'same-password','mysql_user':'new-user','mysql_database':'new-database'}})
            fields={row['field']:row['matched'] for row in result}
            self.assertFalse(fields.get('wordpress.mysql_user',True))
            self.assertFalse(fields.get('wordpress.mysql_database',True))

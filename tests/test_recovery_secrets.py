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

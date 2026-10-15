"""Verification records evidence and limits without enabling applications."""
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'roles/recovery/files'), str(ROOT/'roles/backup/files')]

class RecoveryVerification(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module('recovery_verify')

    def test_authentik_and_n8n_counts_use_snapshot_baseline(self):
        self.assertEqual(self.module.compare_counts({'users': 9}, {'users': 9})['status'], 'matched')
        with self.assertRaises(self.module.BackupError): self.module.compare_counts({'users': 9}, {'users': 8})

    def test_changed_live_counts_do_not_invalidate_snapshot(self):
        result = self.module.compare_counts({'workflows': 0}, {'workflows': 0})
        self.assertEqual(result['baseline'], {'workflows': 0})
        self.assertEqual(self.module.compare_counts({'workflows': 0}, None)['status'], 'baseline_unavailable')

    def test_n8n_wrong_key_fails_without_plaintext_output(self):
        with patch.object(self.module, '_docker', side_effect=self.module.BackupError('Docker failed')):
            with self.assertRaisesRegex(self.module.BackupError, 'n8n credential decryption failed'):
                self.module.check_n8n_cli({'image':'n8n@sha256:'+'a'*64,'name':'recovery-n8n-fixture','network':'recovery-fixture','database':'db','app_user':'app','app_password':'secret','database_host':'db','config_file':'/private/tmp/config','expected_count':1})

    def test_zero_credentials_is_not_applicable(self):
        with patch.object(self.module, '_docker', side_effect=AssertionError('must not execute CLI')):
            self.assertEqual(self.module.check_n8n_cli({'expected_count':0})['status'], 'not_applicable')

    def test_missing_media_blocks_immich_and_aria2_start(self):
        for service in ('immich','aria2','wordpress','pinchflat','mealie'):
            self.assertIn('external_media_not_verified', self.module.media_blockers(service, {}))

    def test_caddy_validation_does_not_start_tls_or_ddns(self):
        with patch.object(self.module, '_docker', return_value=b'') as docker:
            self.module.validate_caddy('caddy@sha256:'+'a'*64, '/private/tmp/Caddyfile')
        args = docker.call_args.args[0]
        self.assertIn('none',args); self.assertIn('validate',args)
        self.assertNotIn('--publish',args)

    def test_report_never_copies_raw_model_or_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary).resolve()/'report.json'
            self.module.write_report({'run_id':'fixture','phase':'verified','snapshot_id':'a'*64,'secrets':'do-not-copy'}, [{'service':'n8n','checks':{},'limitations':['baseline_unavailable']}],path)
            self.assertNotIn('do-not-copy',path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777,0o600)

class RecoveryWiring(unittest.TestCase):
    def test_restore_stops_databases_even_if_verification_fails(self):
        import recovery_runner as runner
        state={'phase':'prepared','services':{'auth':{'phase':'prepared'}},'_run_root':'/private/tmp/fixture','activation':{'services_started':[]}}
        config={'services':['auth'],'target':{},'registry':{},'secrets':{}}
        plan={'model':{},'image_variables':{}}
        state['results']={}
        with patch.object(runner,'save_state'), patch.object(runner,'write_report'), patch.object(runner,'restore_database'), patch.object(runner,'verify_service',side_effect=runner.BackupError('fixture fail')), patch.object(runner,'stop_recovery_databases') as stop:
            with self.assertRaises(runner.BackupError): runner.restore_and_verify(config,plan,state,False)
        stop.assert_called_once_with(state)

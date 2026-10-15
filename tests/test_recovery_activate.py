"""Starting applications is a separate, fenced handoff."""
import copy
import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'roles/recovery/files'),str(ROOT/'roles/backup/files')]
class Activation(unittest.TestCase):
    def setUp(self):
        self.module=importlib.import_module('recovery_activate')
        self.state={'run_id':'fixture','phase':'verified','services':{'n8n':{'phase':'verified'},'proxy':{'phase':'verified'}},'results':{'n8n':{'activation_blockers':[]},'proxy':{'activation_blockers':[]}},'activation':{'services_started':[]},'_state_file':'/unused'}
    def test_activate_requires_matching_verified_run_and_fencing(self):
        for fenced in (False,True):
            state=copy.deepcopy(self.state)
            if fenced: state['phase']='imported'
            with self.assertRaises(self.module.BackupError): self.module.build_activation_plan(state,['n8n'],fenced)
    def test_missing_dependencies_or_media_block_requested_service(self):
        self.state['results']['n8n']['activation_blockers']=['external_media_not_verified']
        with self.assertRaises(self.module.BackupError): self.module.build_activation_plan(self.state,['n8n'],True)
        self.state['results']['n8n']['activation_blockers']=[]
        with self.assertRaises(self.module.BackupError): self.module.build_activation_plan(self.state,['proxy'],True)
    def test_recovery_db_stops_before_production_db_starts(self):
        with patch.object(self.module,'stop_recovery_databases') as stop,patch.object(self.module,'save_state'):
            self.module.begin_activation(self.state,['n8n'],True,'powered off original host')
        stop.assert_called_once_with(self.state)
        self.assertIn('n8n',self.state['activation']['services_started'])
    def test_activation_stops_after_first_failed_ready_check(self):
        with patch.object(self.module,'ready',side_effect=self.module.BackupError('not ready')) as ready:
            with self.assertRaises(self.module.BackupError): self.module.check_activation_ready(['n8n','proxy'],{'stack_root':'/fixture'})
        self.assertEqual(ready.call_count,1)
    def test_no_automatic_dns_rollback_or_old_host_restart(self):
        result=self.module.handoff_report(self.state,False)
        self.assertEqual(result['status'],'awaiting_cutover')
        self.assertFalse(result['dns_changed'])
    def test_new_backup_and_healthchecks_not_inherited_from_old_host(self):
        result=self.module.handoff_report(self.state,True)
        self.assertTrue(result['new_backup_configuration_required'])
        self.assertTrue(result['independent_healthchecks_required'])

class ActivationConstraints(unittest.TestCase):
    def test_domain_and_image_os_are_preserved(self):
        import recovery_activate as module
        config={'domain':'example.test','activation':{'accepted_limitations':{}}}
        plan={'model':{'domain':'old.test','images':{'n8n':[{'os':'linux'}]}}}
        state={'results':{'n8n':{'limitations':[],'activation_blockers':[]}}}
        with self.assertRaises(module.BackupError): module.validate_activation_environment(config,plan,state,['n8n'])
        plan['model']['domain']='example.test'; plan['model']['images']['n8n'][0]['os']='windows'
        with self.assertRaises(module.BackupError): module.validate_activation_environment(config,plan,state,['n8n'])

    def test_limited_baseline_requires_explicit_review_reference(self):
        import recovery_activate as module
        config={'domain':'example.test','activation':{'accepted_limitations':{}}}
        plan={'model':{'domain':'example.test','images':{'n8n':[{'os':'linux'}]}}}
        state={'results':{'n8n':{'limitations':['snapshot_count_baseline_unavailable'],'activation_blockers':[]}}}
        with self.assertRaises(module.BackupError): module.validate_activation_environment(config,plan,state,['n8n'])
        config['activation']['accepted_limitations']['n8n']='Reviewed protected count report and tested credential decryption'
        module.validate_activation_environment(config,plan,state,['n8n'])

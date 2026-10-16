"""Durable host-wide claims survive empty directories, new state roots and crashes."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'roles/recovery/files'),str(ROOT/'roles/backup/files')]
import recovery_runner as runner
import recovery_state as states

class CrossRunOwnership(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.addCleanup(patch.stopall)
        patch.object(states,'OWNERSHIP_FILE',self.root/'host/ownership.json',create=True).start()
        self.destination=self.root/'storage/homepage/config'
        selection={'snapshot_id':'a'*64,'tree_root':'/','manifest_path':'/manifest.json'}
        self.plan={'run_id':'run-a','selection':selection,'profile_sha256':'b'*64,'target':{'machine_id':'fixture'},'services':['homepage'],'approved_roots':[str(self.destination)],'image_variables':{},
            'model':{'selection':selection,'schema_version':1,'sources':{'homepage-config':{'kind':'files','artifacts':[{'relative_path':'homepage-config/settings.yaml','entry_type':'file','size':8,'sha256':hashlib.sha256(b'key: ok\n').hexdigest()}]}}}}
        self.config={'state_root':str(self.root/'state-a'),'target':{'machine_id':'fixture','storage_root':str(self.root/'storage'),'storage_mount':'/'},'services':['homepage'],'source':{},'repository':{},'secrets':{},'registry':{'homepage':{'sources':{'homepage-config':{'kind':'files','layout':'file','target':'homepage/config/settings.yaml'}}}}}
        def transport(repo,args,**kwargs):
            scratch=Path(args[args.index('--target')+1]);(scratch/'homepage-config').mkdir(parents=True,exist_ok=True);(scratch/'homepage-config/settings.yaml').write_text('key: ok\n');return b''
        patch.object(runner,'select_snapshot',return_value=selection).start()
        patch.object(runner,'restic',side_effect=transport).start()
        patch.object(runner,'_docker',return_value=b'').start()

    def test_two_sequential_downloads_cannot_claim_same_empty_root(self):
        runner.download(self.config,self.plan)
        second=deepcopy(self.plan);second['run_id']='run-b'
        config={**self.config,'state_root':str(self.root/'different-state')}
        with self.assertRaises(runner.BackupError):runner.download(config,second)

    def test_foreign_cleanup_cannot_delete_another_runs_data(self):
        runner.download(self.config,self.plan)
        marker=self.destination/'imported-data';marker.write_text('belongs to A')
        second=deepcopy(self.plan);second['run_id']='run-b'
        state=states.open_run(second,self.root/'different-state')
        state['owned_paths']=[str(self.destination)]
        with patch.object(runner,'_docker',return_value=b''),self.assertRaises(runner.BackupError):runner.cleanup_run(state)
        self.assertEqual(marker.read_text(),'belongs to A')

    def test_foreign_running_container_blocks_cleanup_of_owned_root(self):
        state=runner.download(self.config,self.plan)
        marker=self.destination/'data';marker.touch()
        def docker(args,**kwargs):
            if args[0]=='ps':return b'foreign-container'
            if args[0]=='inspect':return json.dumps({'run':'other-run','mounts':[{'Source':str(self.destination)}]}).encode()
            return b''
        with patch.object(runner,'_docker',side_effect=docker),self.assertRaises(runner.BackupError):runner.cleanup_run(state)
        self.assertTrue(marker.exists())

    def test_cleanup_releases_claim_for_a_later_run(self):
        state=runner.download(self.config,self.plan)
        runner.cleanup_run(state)
        second=deepcopy(self.plan);second['run_id']='run-b'
        result=runner.download({**self.config,'state_root':str(self.root/'different-state')},second)
        self.assertEqual(result['run_id'],'run-b')
        self.assertEqual((self.root/'host/ownership.json').stat().st_mode&0o777,0o600)

    def test_claim_survives_crash_before_run_records_owned_paths(self):
        state=states.open_run(self.plan,Path(self.config['state_root']))
        states.reserve_paths(state)
        self.assertEqual(state['owned_paths'],[])
        self.destination.mkdir(parents=True);(self.destination/'partial').touch()
        self.assertIn(str(self.destination),[row['value'] for row in states.cleanup_preview(state)])
        runner.cleanup_run(state)
        self.assertFalse(self.destination.exists())

    def test_overlapping_parent_cannot_be_reserved_by_other_run(self):
        runner.download(self.config,self.plan)
        second=deepcopy(self.plan);second['run_id']='run-b';second['approved_roots']=[str(self.destination.parent)]
        state=states.open_run(second,self.root/'other-state')
        with self.assertRaises(runner.BackupError):states.reserve_paths(state)

class ResumeSecrets(unittest.TestCase):
    setUp=CrossRunOwnership.setUp
    def test_resumed_download_rechecks_actual_vault_fields(self):
        definition=self.config['registry'].pop('homepage')
        self.config['registry']['auth']=definition
        self.config['services']=['auth'];self.plan['services']=['auth']
        self.config['secrets']={'authentik':{'pgpass':'fixture-old-password','secret_key':'fixture-old-key'}}
        def transport(repo,args,**kwargs):
            scratch=Path(args[args.index('--target')+1]);(scratch/'homepage-config').mkdir(parents=True,exist_ok=True)
            (scratch/'homepage-config/settings.yaml').write_text('key: ok\n')
            (scratch/'auth-compose').mkdir(exist_ok=True)
            (scratch/'auth-compose/docker-compose.yml').write_text('services:\n  postgresql:\n    image: postgres:fixed\n    environment:\n      POSTGRES_PASSWORD: fixture-old-password\n  authentik-server:\n    image: auth:fixed\n    environment:\n      AUTHENTIK_SECRET_KEY: fixture-old-key\n')
            return b''
        with patch.object(runner,'restic',side_effect=transport):state=runner.download(self.config,self.plan)
        self.assertEqual(state['phase'],'downloaded')
        self.config['secrets']['authentik']['secret_key']='fixture-new-key'
        with self.assertRaises(runner.BackupError):runner.download(self.config,self.plan)
        persisted=Path(state['_state_file']).read_text()
        self.assertNotIn('fixture-old-key',persisted);self.assertNotIn('fixture-new-key',persisted)

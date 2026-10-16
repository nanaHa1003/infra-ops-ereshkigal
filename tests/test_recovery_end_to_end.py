"""Full backend file+SQLite restore with deleted source, for legacy and v2."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from recovery_fixtures import sqlite_repository
import recovery_state
from recovery_runner import create_plan,load_plan,download,restore_and_verify
from backup_common import restic

@unittest.skipUnless(shutil.which('restic'),'restic required for real repository test')
class RecoveryEndToEnd(unittest.TestCase):
    def roundtrip(self,version):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve()
            with patch.object(recovery_state,'OWNERSHIP_FILE',root/'host/ownership.json'),patch('recovery_runner._docker',return_value=b''),patch.dict(os.environ,{'RESTIC_CACHE_DIR':str(root/'cache')}):
                config=sqlite_repository(root,version)
                before=json.loads(restic(config['repository'],['snapshots','--json'],capture=True))
                result=create_plan(config);self.assertEqual(result['blockers'],[])
                plan=load_plan(config)
                state=download(config,plan)
                self.assertEqual(state['phase'],'downloaded')
                result=restore_and_verify(config,plan,state,False)
                self.assertEqual(result['phase'],'verified')
                database=root/'replacement-storage/mealie/data/mealie.db'
                with sqlite3.connect(database) as db:self.assertEqual(db.execute('SELECT name FROM recipes').fetchone()[0],'synthetic recipe')
                self.assertEqual(database.stat().st_uid,int(os.environ.get('HOMELAB_FIXTURE_UID',os.getuid())))
                first=database.read_bytes()
                restore_and_verify(config,plan,state,True)
                self.assertEqual(first,database.read_bytes())
                report=json.loads(Path(result['report']).read_text())
                mealie=next(row for row in report['services'] if row['service']=='mealie')
                self.assertIn('external_media_not_verified',mealie['activation_blockers'])
                self.assertEqual(mealie['checks']['counts']['status'],'matched' if version==2 else 'baseline_unavailable')
                self.assertFalse((root/'old-host').exists())
                self.assertEqual(before,json.loads(restic(config['repository'],['snapshots','--json'],capture=True)))
                self.assertNotIn('synthetic-repository-password',Path(result['report']).read_text())
    def test_v1_source_gone_new_host_paths(self):self.roundtrip(1)
    def test_v2_source_gone_new_host_paths(self):self.roundtrip(2)

"""Real disposable Restic snapshots prove isolation and direct-media recoverability."""
import copy,hashlib,importlib,json,os,shutil,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'roles/backup/files'),str(ROOT/'roles/recovery/files')]

@unittest.skipUnless(shutil.which('restic'),'restic binary required')
class IndependentRoundTrips(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
        self.runner=importlib.import_module('backup_runner');self.recovery=importlib.import_module('recovery_manifest');self.files=importlib.import_module('recovery_files')
        self.media=self.root/'source';self.media.mkdir();(self.media/'photo.jpg').write_bytes(os.urandom(4096));(self.media/'manifest.json').write_text('user file, not recovery metadata')
        (self.media/'nested').mkdir();(self.media/'nested/mealie.db').write_bytes(b'ordinary attachment')
        with sqlite3.connect(self.media/'mealie.db') as db:db.execute('CREATE TABLE recipes(id INTEGER)');db.execute('INSERT INTO recipes VALUES (1)')
        self.sources=[{'id':'mealie-db','kind':'sqlite','path':str(self.media/'mealie.db')},{'id':'mealie-data','kind':'files','capture':'direct','path':str(self.media),'exclude':['mealie.db','mealie.db-wal','mealie.db-shm']}]
        repositories={name:{'url':str(self.root/('repo-'+name)),'access_key_id':'dummy','secret_access_key':'dummy','restic_password':'test-'+name} for name in ('database','media')}
        self.cfg={'state_root':str(self.root/'state'),'host':'old-host','repositories':repositories,'jobs':{'database':{'repository':'database','healthchecks_enabled':False,'sources':[{'id':'settings','kind':'files','path':str(self.media/'manifest.json')}]},'media':{'repository':'media','healthchecks_enabled':False,'sources':self.sources}}}
        for repo in repositories.values():self.runner.restic(repo,['init'])

    def test_media_restores_with_staged_sqlite_and_nested_manifest(self):
        live=sqlite3.connect(self.media/'mealie.db')
        self.addCleanup(live.close)
        live.execute('PRAGMA journal_mode=WAL')
        live.execute('UPDATE recipes SET id=1');live.commit()
        self.assertTrue((self.media/'mealie.db-wal').exists())
        self.assertTrue((self.media/'mealie.db-shm').exists())
        self.assertEqual(self.runner.run_job(self.cfg,'media'),0)
        scope={'repository':'media','host':'old-host','job':'media','staging_path':str(self.root/'state/staging/media'),'snapshot':'latest'}
        selection=self.recovery.select_snapshot(self.cfg['repositories']['media'],scope)
        raw=json.loads(self.runner.restic(self.cfg['repositories']['media'],['dump',selection['snapshot_id'],selection['manifest_path']],capture=True))
        model=self.recovery.normalize_manifest(raw,selection,{})
        restored=self.runner.restore_job(self.cfg,'media','latest')
        direct_tree=restored/str(self.media).lstrip('/')
        for name in ('mealie.db','mealie.db-wal','mealie.db-shm'):
            self.assertFalse((direct_tree/name).exists(),name+' must not be archived from the live database')
        target={'storage_root':str(self.root/'replacement'),'storage_mount':'/','scratch_root':str(restored),'services':['mealie'],'run_id':'run'}
        registry={'mealie':{'sources':{'mealie-db':{'kind':'sqlite','layout':'file','target':'mealie/data/mealie.db','owner':os.getuid(),'group':os.getgid(),'mode':'0600'},'mealie-data':{'kind':'files','layout':'directory','target':'mealie/data','owner':os.getuid(),'group':os.getgid(),'mode':'0600'}}}}
        state={'run_id':'run','owned_paths':[],'restic_verified_snapshot':selection['snapshot_id']}
        self.files.install_artifacts(self.files.build_mappings(model,target,registry),restored,state)
        destination=self.root/'replacement/mealie/data'
        for name in ('photo.jpg','manifest.json','nested/mealie.db'):
            self.assertEqual(hashlib.sha256((destination/name).read_bytes()).hexdigest(),hashlib.sha256((self.media/name).read_bytes()).hexdigest())
        with sqlite3.connect(destination/'mealie.db') as db:self.assertEqual(db.execute('SELECT count(*) FROM recipes').fetchone()[0],1)
        self.assertFalse((self.root/'state/staging/media').exists())
        self.runner.restic(self.cfg['repositories']['media'],['check','--read-data'])

    def test_real_process_holding_local_lock_does_not_block_database(self):
        script='import json,sys; import backup_runner as b; c=json.loads(sys.stdin.readline());\nwith b.repository_lock(c,"media"):\n print("ready",flush=True);sys.stdin.read(1)'
        env={**os.environ,'PYTHONPATH':str(ROOT/'roles/backup/files')}
        child=subprocess.Popen([sys.executable,'-c',script],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
        try:
            child.stdin.write(json.dumps(self.cfg)+'\n');child.stdin.flush();self.assertEqual(child.stdout.readline().strip(),'ready')
            self.assertEqual(self.runner.run_job(self.cfg,'database'),0)
            self.assertNotEqual(self.runner.run_job(self.cfg,'media'),0)
        finally:
            child.stdin.write('x');child.stdin.flush();child.communicate(timeout=10)
        self.assertEqual(len(self.runner.snapshots(self.cfg,'database')),1)

    def test_destination_failures_are_independent_in_both_directions(self):
        broken=copy.deepcopy(self.cfg);broken['repositories']['media']['restic_password']='wrong'
        self.assertNotEqual(self.runner.run_job(broken,'media'),0)
        self.assertEqual(self.runner.run_job(broken,'database'),0)
        broken=copy.deepcopy(self.cfg);broken['repositories']['database']['url']=str(self.root/'missing-repository')
        self.assertNotEqual(self.runner.run_job(broken,'database'),0)
        self.assertEqual(self.runner.run_job(broken,'media'),0)

    def test_remote_monitoring_failure_cannot_change_local_success(self):
        self.cfg['jobs']['database'].update(healthchecks_enabled=True,healthchecks_ping_url='https://hc-ping.com/00000000-0000-0000-0000-000000000000')
        with patch.object(self.runner.urllib.request,'urlopen',side_effect=OSError('offline')):
            self.assertEqual(self.runner.run_job(self.cfg,'database'),2)
            self.assertEqual(self.runner.run_job(self.cfg,'media'),0)

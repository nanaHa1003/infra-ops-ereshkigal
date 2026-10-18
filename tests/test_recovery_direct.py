"""v3 direct artifacts recover through verified scratch, never original host paths."""
import copy,hashlib,importlib,json,os,shutil,sqlite3,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import yaml
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'roles/recovery/files'),str(ROOT/'roles/backup/files')]

class DirectRecovery(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name).resolve()
        self.data=self.root/'old/data';self.data.mkdir(parents=True);(self.data/'photo.jpg').write_bytes(b'photo')
        with sqlite3.connect(self.data/'mealie.db') as db:db.execute('CREATE TABLE recipes(id INTEGER)')
        self.sources=[{'id':'mealie-db','kind':'sqlite','path':str(self.data/'mealie.db')},{'id':'mealie-data','kind':'files','capture':'direct','path':str(self.data),'exclude':['mealie.db','mealie.db-wal','mealie.db-shm']}]
        import backup_sources,backup_manifest,recovery_manifest,recovery_files
        self.manifest=recovery_manifest;self.files=recovery_files
        self.stage=self.root/'stage';exports=backup_sources.prepare_sources(self.sources,self.stage)
        self.raw=backup_manifest.build_manifest(self.stage,{'job_id':'full','host':'old','jobs':{'full':{'sources':self.sources}}},exports)
        self.selection={'host':'old','job':'full','tree_root':str(self.stage),'snapshot_id':'a'*64,'staging_path':str(self.stage),'snapshot_paths':[str(self.stage),str(self.data)]}
        self.scratch=self.root/'scratch';shutil.copytree(self.stage,self.scratch/str(self.stage).lstrip('/'));shutil.copytree(self.data,self.scratch/str(self.data).lstrip('/'))
        self.registry={'mealie':{'sources':{'mealie-db':{'kind':'sqlite','layout':'file','target':'mealie/data/mealie.db','owner':os.getuid(),'group':os.getgid(),'mode':'0600'},'mealie-data':{'kind':'files','layout':'directory','target':'mealie/data','owner':os.getuid(),'group':os.getgid(),'mode':'0600','required':False}}}}
        self.target={'storage_root':str(self.root/'replacement'),'storage_mount':'/','scratch_root':str(self.scratch),'services':['mealie'],'run_id':'test'}
        self.state={'run_id':'test','owned_paths':[],'restic_verified_snapshot':'a'*64}

    def model(self):return self.manifest.normalize_manifest(self.raw,self.selection,{})

    def test_direct_and_sqlite_restore_to_new_root(self):
        mappings=self.files.build_mappings(self.model(),self.target,self.registry)
        self.files.install_artifacts(mappings,self.scratch,self.state)
        self.assertEqual((self.root/'replacement/mealie/data/photo.jpg').read_bytes(),b'photo')
        with sqlite3.connect(self.root/'replacement/mealie/data/mealie.db') as db:self.assertEqual(db.execute('SELECT count(*) FROM recipes').fetchone()[0],0)

    def test_direct_artifacts_require_verified_snapshot(self):
        mappings=self.files.build_mappings(self.model(),self.target,self.registry)
        self.state.pop('restic_verified_snapshot')
        with self.assertRaises(self.files.BackupError):self.files.install_artifacts(mappings,self.scratch,self.state)

    def test_missing_staged_hash_is_still_rejected(self):
        self.raw['sources']['mealie-db']['artifacts'][-1]['sha256']=None
        with self.assertRaises(self.files.BackupError):self.model()

    def test_snapshot_root_must_match_source_and_not_escape(self):
        self.raw['sources']['mealie-data']['snapshot_root']='/etc'
        with self.assertRaises(self.files.BackupError):self.model()

    def test_direct_file_cannot_collide_with_database(self):
        self.raw['sources']['mealie-data']['artifacts'].append({'relative_path':'mealie-data/mealie.db','entry_type':'file','size':1,'sha256':None})
        with self.assertRaises(self.files.BackupError):self.files.build_mappings(self.model(),self.target,self.registry)

    def test_aria2_session_is_optional_in_registry(self):
        registry=yaml.safe_load((ROOT/'roles/recovery/vars/services.yml').read_text())['recovery_service_registry']
        self.assertFalse(registry['aria2']['sources']['aria2-session']['required'])

    def test_scoped_manifest_wins_over_media_file_named_manifest(self):
        scope={'repository':'local','host':'old','job':'full','snapshot':'latest','staging_path':str(self.stage),'snapshot_paths':[str(self.stage),str(self.data)]}
        snap={'id':'a'*64,'hostname':'old','time':'2026-10-18T01:00:00Z','tags':['homelab-config-db:full'],'paths':[str(self.stage),str(self.data)]}
        nodes='\n'.join(json.dumps({'type':'file','path':str(p)}) for p in (self.stage/'manifest.json',self.data/'manifest.json'))
        def transport(repo,args,**kwargs):return (json.dumps([snap]) if args[0]=='snapshots' else nodes).encode()
        with patch.object(self.manifest,'restic',side_effect=transport):
            self.assertEqual(self.manifest.select_snapshot({},scope)['manifest_path'],str(self.stage/'manifest.json'))

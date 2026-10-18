"""Direct source inventory must not copy media or misstate archived hashes."""
import importlib
from pathlib import Path
import sys,tempfile,unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'roles/backup/files'))

class DirectCapture(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.media=self.root/'media';self.media.mkdir()
        (self.media/'photo.jpg').write_bytes(b'original')
        (self.media/'mealie.db').write_bytes(b'live-db')
        self.source={'id':'media','kind':'files','capture':'direct','path':str(self.media),'exclude':['mealie.db','mealie.db-wal','mealie.db-shm']}
        self.sources=importlib.import_module('backup_sources');self.manifest=importlib.import_module('backup_manifest')

    def prepare(self):
        return self.sources.prepare_sources([self.source],self.root/'stage')

    def test_media_is_not_copied_into_staging(self):
        exports=self.prepare()
        self.assertFalse((self.root/'stage/media').exists())
        self.assertEqual(exports['media']['capture'],'direct')

    def test_v3_manifest_describes_direct_root_without_inventing_hash(self):
        exports=self.prepare();config={'job_id':'local','host':'host','jobs':{'local':{'sources':[self.source]}}}
        m=self.manifest.build_manifest(self.root/'stage',config,exports)
        self.assertEqual(m['schema_version'],3)
        rec=m['sources']['media'];self.assertEqual(rec['snapshot_root'],str(self.media))
        paths=[a['relative_path'] for a in rec['artifacts']]
        self.assertIn('media/photo.jpg',paths);self.assertNotIn('media/mealie.db',paths)
        self.assertIsNone(next(a for a in rec['artifacts'] if a['relative_path']=='media/photo.jpg')['sha256'])
        self.assertEqual(rec['verification']['status'],'restic_authenticated_content')

    def test_symlink_is_rejected_before_upload(self):
        (self.media/'escape').symlink_to(self.root)
        with self.assertRaises(self.sources.BackupError):self.prepare()

    def test_mutation_is_detected_before_completion(self):
        exports=self.prepare();(self.media/'photo.jpg').write_bytes(b'changed')
        direct=importlib.import_module('backup_direct')
        with self.assertRaises(self.sources.BackupError):direct.verify_direct([self.source],exports)

    def test_missing_required_root_fails_but_optional_absence_is_recorded(self):
        self.source['path']=str(self.root/'absent')
        with self.assertRaises(self.sources.BackupError):self.prepare()
        self.source['optional']=True;self.assertTrue(self.prepare()['media']['optional_absent'])

    def test_mutation_reports_source_and_counts_without_filenames(self):
        exports=self.prepare()
        (self.media/'private-photo-name.jpg').write_bytes(b'new')
        (self.media/'photo.jpg').unlink()
        direct=importlib.import_module('backup_direct')
        with self.assertRaises(self.sources.BackupError) as raised:
            direct.verify_direct([self.source],exports)
        message=str(raised.exception)
        self.assertIn('source=media',message)
        self.assertIn('added=1',message);self.assertIn('removed=1',message)
        self.assertNotIn('private-photo-name',message)

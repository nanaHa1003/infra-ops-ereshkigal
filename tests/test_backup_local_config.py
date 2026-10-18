"""Production local jobs have explicit scope, no monitoring and per-job units."""
import json
from pathlib import Path
import unittest
from jinja2 import Environment,StrictUndefined
import yaml
import test_backup_ansible as fixtures
ROOT=Path(__file__).resolve().parents[1]

class LocalConfiguration(unittest.TestCase):
    def config(self):return fixtures.BackupConfiguration.render_config(self)

    def test_local_jobs_need_no_healthcheck_secrets(self):
        cfg=self.config()
        self.assertEqual(set(cfg['jobs']),{'database','rustfs_db','rustfs_files'})
        for name in ('rustfs_db','rustfs_files'):
            self.assertFalse(cfg['jobs'][name]['healthchecks_enabled'])
            self.assertEqual(cfg['jobs'][name]['healthchecks_ping_url'],'')
        self.assertTrue(cfg['jobs']['database']['healthchecks_enabled'])

    def test_media_scope_includes_databases_but_excludes_aria2_session(self):
        cfg=self.config();job=cfg['jobs']['rustfs_files'];sources={s['id']:s for s in job['sources']}
        self.assertEqual(sources['immich-media']['path'],'/mnt/storage/immich/library')
        self.assertEqual(sources['wordpress-html']['capture'],'direct')
        self.assertEqual(sources['pinchflat-downloads']['capture'],'direct')
        self.assertIn('n8n-key',sources);self.assertIn('mealie-db',sources)
        self.assertIn('mealie.db-wal',sources['mealie-data']['exclude'])
        for job in cfg['jobs'].values():
            self.assertFalse(any(s['kind']=='aria2_session' or '/aria2/downloads' in s.get('path','') for s in job['sources']))

    def test_per_job_template_does_not_include_other_credentials(self):
        self.config();env=Environment(undefined=StrictUndefined)
        env.filters.update(to_json=json.dumps,combine=lambda a,b:{**a,**b})
        cfg=json.loads(env.from_string((ROOT/'roles/backup/templates/config.json.j2').read_text()).render({**self.values,'backup_config_job':'rustfs_files'}))
        self.assertEqual(set(cfg['jobs']),{'rustfs_files'})
        self.assertEqual(set(cfg['repositories']),{'rustfs_files'})

    def test_service_uses_instance_config_and_independent_cache(self):
        self.config();text=(ROOT/'roles/backup/templates/homelab-backup@.service.j2').read_text()
        env=Environment(undefined=StrictUndefined);env.filters['to_json']=json.dumps;env.filters['dirname']=lambda p:str(Path(p).parent)
        rendered=env.from_string(text).render(self.values)
        self.assertIn('/jobs/%i.json',rendered)
        self.assertIn('/cache/%i',rendered)
        self.assertNotIn('Requires=homelab-backup',rendered)

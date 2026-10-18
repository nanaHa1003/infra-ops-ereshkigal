"""Independent destinations must not share validation, locks or monitoring failures."""
import copy
import json
import unittest
from unittest.mock import patch
import test_backup_runner as fixtures


class IndependentJobs(unittest.TestCase):
    def setUp(self):
        fixtures.BackupRunner.setUp(self)
        other = copy.deepcopy(self.config['jobs']['database'])
        other.update(repository='local', healthchecks_enabled=False)
        self.config['jobs']['local'] = other
        self.config['repositories']['local'] = {**self.config['repositories']['database'], 'url': str(self.root/'other-repo')}

    def test_unrelated_invalid_credentials_do_not_block_selected_job(self):
        self.config['repositories']['local']['restic_password'] = ''
        self.config['jobs']['local']['sources'][0]['path'] = '/not-present'
        self.assertEqual(self.runner.run_job(self.config, 'database'), 0)

    def test_independent_repository_lock_allows_another_backup(self):
        with self.runner.repository_lock(self.config, 'local'):
            self.assertEqual(self.runner.run_job(self.config, 'database'), 0)

    def test_aliases_of_same_repository_conflict(self):
        self.config['repositories']['local']['url'] = self.config['repositories']['database']['url']+'/'
        with self.runner.repository_lock(self.config, 'local'):
            self.assertNotEqual(self.runner.run_job(self.config, 'database'), 0)

    def test_disabled_monitoring_does_not_validate_or_send_stale_url(self):
        self.config['jobs']['local']['healthchecks_ping_url'] = 'http://must-not-be-contacted.test/'
        with patch.object(self.runner.urllib.request, 'urlopen', side_effect=AssertionError('unexpected monitoring')):
            self.assertEqual(self.runner.run_job(self.config, 'local'), 0)

    def test_success_state_is_written_only_for_successful_job(self):
        self.assertEqual(self.runner.run_job(self.config, 'database'), 0)
        state=self.root/'state/status/database.json'
        self.assertEqual(json.loads(state.read_text())['job'], 'database')
        self.assertFalse((self.root/'state/status/local.json').exists())

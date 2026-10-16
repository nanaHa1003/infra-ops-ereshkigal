"""Actual read-only probe on a WSL-like Linux host without systemd/SELinux."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

class RecoveryProbe(unittest.TestCase):
    def test_no_systemd_or_selinux_needed_and_debian_root_cron_is_detected(self):
        calls = []
        def run(args, **kwargs):
            calls.append(args)
            if 'info' in args:
                output = 'linux\n'
            elif 'compose' in args:
                output = '2.39.0\n'
            elif args[:2] == ['restic', 'version']:
                output = 'restic 0.19.1\n'
            else:
                output = ''
            return subprocess.CompletedProcess(args, 0, output, '')
        with tempfile.TemporaryDirectory() as directory:
            config = {'storage_root': directory, 'stack_root': directory + '/stacks', 'storage_mount': '/', 'services': [], 'registry': {}}
            original_exists, original_read, original_file = Path.exists, Path.read_text, Path.is_file
            def exists(path):
                if str(path) == '/var/spool/cron/crontabs/root': return True
                if str(path) in ('/var/spool/cron/root', '/sys/fs/selinux/enforce', '/run/systemd/system'): return False
                return original_exists(path)
            def read(path, *args, **kwargs):
                if str(path) == '/var/spool/cron/crontabs/root': return '* * * * * /usr/local/bin/ddupdate.sh\n'
                return original_read(path, *args, **kwargs)
            def is_file(path):
                return str(path) == "/var/spool/cron/crontabs/root" or original_file(path)
            output = io.StringIO()
            with patch('sys.stdin', io.StringIO(json.dumps(config))), contextlib.redirect_stdout(output), patch('subprocess.run', side_effect=run), patch('shutil.which', side_effect=lambda name: '/usr/bin/' + name if name in ('docker', 'restic') else None), patch.object(Path, 'exists', exists), patch.object(Path, 'is_file', is_file), patch.object(Path, 'read_text', read):
                runpy.run_path(str(ROOT/'roles/recovery/files/recovery_probe.py'), run_name='__main__')
            result = json.loads(output.getvalue())
            self.assertFalse(result['service_probe_failed'])
            self.assertTrue(result['ddns_active'])
            self.assertFalse(result['active_backup_timers'])
            self.assertTrue(result['capabilities']['compose_v2'])
            self.assertTrue(result['capabilities']['selinux_tools'])
            self.assertFalse(any(call[0] == 'systemctl' for call in calls))

if __name__ == '__main__': unittest.main()

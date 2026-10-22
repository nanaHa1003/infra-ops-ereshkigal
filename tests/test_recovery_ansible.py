"""Exercise recovery host selection through Ansible's actual play entry."""
import os
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/"roles/recovery/files"),str(ROOT/"roles/backup/files")]

class RecoveryAnsible(unittest.TestCase):
    def setUp(self):
        self.assertTrue((ROOT / "recover.yml").exists(), "missing standalone recovery play")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scratch = Path(self.temp.name).resolve()
        self.inventory = self.scratch / "hosts.ini"
        self.inventory.write_text("[recovery_targets]\none ansible_connection=local\ntwo ansible_connection=local\n")
        self.config = self.scratch / "ansible.cfg"
        self.config.write_text(f"[defaults]\nroles_path={ROOT / 'roles'}\nlocal_tmp={self.scratch / 'local-tmp'}\nremote_tmp={self.scratch / 'remote-tmp'}\n[privilege_escalation]\nbecome_ask_pass=false\n")

    def run_play(self, *args):
        executable = shutil.which("ansible-playbook") or str(Path(sys.executable).parent / "ansible-playbook")
        env = {**os.environ, "ANSIBLE_CONFIG": str(self.config), "ANSIBLE_NOCOLOR": "1"}
        env.pop("ANSIBLE_VAULT_PASSWORD_FILE", None)
        return subprocess.run([executable, str(ROOT / "recover.yml"), "-i", str(self.inventory),
            "-e", "ansible_become=false", *args], capture_output=True, text=True, cwd=self.scratch, env=env, timeout=60)

    def test_only_one_explicit_recovery_target_allowed(self):
        result = self.run_play("--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Exactly one recovery target", result.stdout)
        self.assertNotIn("Install recovery", result.stdout)

    def test_recover_never_imports_site_or_backup_main(self):
        result = self.run_play("--list-tasks", "--limit", "one")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Validate recovery request", result.stdout)
        self.assertNotIn("Start/Update", result.stdout)
        self.assertNotIn("Deploy deSEC", result.stdout)
        self.assertNotIn("Enable or disable timers", result.stdout)

    def test_check_mode_does_not_import_or_activate(self):
        profile = self.scratch / "profile.json"
        plan = self.scratch / "plan.json"
        profile.write_text(json.dumps({"recover_phase": "restore", "recover_plan_file": str(plan),
            "recover_services": ["homepage"], "recover_source": {"repository": "database", "host": "old.example", "job": "database", "staging_path": "/old/staging", "snapshot": "latest"},
            "recover_repositories": {"database": {"url": str(self.scratch / "unavailable-repository")}},
            "storage_root": str(self.scratch / "data"), "stack_root": str(self.scratch / "stack"), "recover_storage_mount": "/", "recover_min_free_bytes": 1,
            "domain": "test.example", "vault_secrets": {"backup": {"repositories": {"database": {"access_key_id": "fixture-key", "secret_access_key": 'fixture-quote"\nvalue', "restic_password": "fixture-pass"}}}}}))
        result = self.run_play("--check", "--limit", "one", "-e", "@" + str(profile))
        self.assertIn("Show recovery plan outcome", result.stdout, result.stdout + result.stderr)
        self.assertNotIn("Install recovery dependencies", result.stdout)
        self.assertNotIn("Restore selected service data", result.stdout)
        self.assertNotIn("fixture-quote", result.stdout + result.stderr)
        self.assertFalse((self.scratch / "data").exists())
        self.assertTrue(plan.exists(), "check mode did not produce a preflight report")
        self.assertTrue(json.loads(plan.read_text())["blockers"])

    @unittest.skipUnless(os.geteuid()==0,'root-only dynamic prepare regression runs in Molecule recovery')
    def test_actual_dynamic_prepare_renders_all_services_without_starting(self):
        from recovery_runner import IMAGE_VARIABLES
        source=yaml.safe_load((ROOT/'roles/recovery/tasks/prepare.yml').read_text())
        tasks=[task for task in source if 'ansible.builtin.include_role' in task and 'vars' in task]
        self.assertEqual(len(tasks),9)
        variables=yaml.safe_load((ROOT/'inventory/group_vars/all/vault.ci.yml').read_text())
        variables.update({'domain':'fixture.test','timezone':'UTC','storage_root':str(self.scratch/'data'),'stack_root':str(self.scratch/'stack'),
            'recover_services':list(IMAGE_VARIABLES),'recovery_pinned_plan':{
                'image_variables':{name:'fixture/'+name+'@sha256:'+'a'*64 for names in IMAGE_VARIABLES.values() for name in names.values()},
                'model':{'sources':{'immich-db':{'version':'PostgreSQL 18.0'}}},
            }})
        play=self.scratch/'prepare.yml'
        play.write_text(yaml.safe_dump([{'name':'Execute actual recovery prepare role includes','hosts':'one','gather_facts':False,'vars':variables,'tasks':tasks}],sort_keys=False))
        executable=shutil.which('ansible-playbook') or str(Path(sys.executable).parent/'ansible-playbook')
        result=subprocess.run([executable,str(play),'-i',str(self.inventory),'--skip-tags','molecule-notest'],cwd=self.scratch,env={**os.environ,'ANSIBLE_CONFIG':str(self.config),'ANSIBLE_NOCOLOR':'1'},capture_output=True,text=True,timeout=90)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertNotIn('Start/Update',result.stdout)
        for service in IMAGE_VARIABLES:
            path=self.scratch/'stack'/service/'docker-compose.yml'
            self.assertTrue(path.exists(),str(path))
            compose=yaml.safe_load(path.read_text())
            self.assertTrue(all('@sha256:' in value['image'] for value in compose['services'].values()))
            if service=='immich':
                database=compose['services']['database']
                self.assertTrue(any(mount.endswith(':/var/lib/postgresql:Z') for mount in database['volumes']))

    def test_host_integration_skips_selinux_and_does_not_bootstrap_distribution(self):
        install = yaml.safe_load((ROOT/'roles/recovery/tasks/install.yml').read_text())
        prepare = yaml.safe_load((ROOT/'roles/recovery/tasks/prepare.yml').read_text())
        # Execute the public host guard and actual SELinux tasks on a non-SELinux
        # target; no sudo, package installation, Docker or root chown is needed.
        tasks = [install[0]] + [task for task in prepare if task['name'] in ('Label replacement service storage', 'Apply replacement SELinux labels')]
        play = self.scratch/'portable.yml'
        capabilities = dict.fromkeys(('linux', 'docker', 'compose_v2', 'restic', 'pyyaml', 'selinux_tools'), True)
        play.write_text(yaml.safe_dump([{'hosts': 'one', 'gather_facts': False, 'vars': {
            'ansible_facts': {'distribution': 'Ubuntu', 'distribution_major_version': '24', 'selinux': {'status': 'disabled'}},
            'recovery_probe': {'capabilities': capabilities, 'mount_ok': True, 'service_probe_failed': False,
                'active_backup_timers': False, 'ddns_active': False, 'free_bytes': 1000},
            'recover_min_free_bytes': 100, 'storage_root': str(self.scratch/'must-not-create')}, 'tasks': tasks}], sort_keys=False))
        executable = shutil.which('ansible-playbook') or str(Path(sys.executable).parent/'ansible-playbook')
        result = subprocess.run([executable, str(play), '-i', str(self.inventory)], cwd=self.scratch,
            env={**os.environ, 'ANSIBLE_CONFIG': str(self.config), 'ANSIBLE_NOCOLOR': '1'}, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('skipped=2', result.stdout)
        self.assertFalse((self.scratch/'must-not-create').exists())
        self.assertFalse(any('ansible.builtin.package' in task or 'ansible.builtin.include_role' in task for task in install))

    def test_invalid_phase_is_rejected_before_bootstrap(self):
        result = self.run_play("--check", "--limit", "one", "-e", "recover_phase=unsafe")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Unsupported recovery phase", result.stdout)
        self.assertNotIn("Install recovery", result.stdout)

if __name__ == "__main__": unittest.main()

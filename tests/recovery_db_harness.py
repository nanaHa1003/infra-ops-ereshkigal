"""Run the integration suite inside a Docker-CLI container on a test engine.

The outer daemon must be disposable and explicitly chosen by the operator.
Only synthetic fixtures and Python modules are transferred; never inventory/Vault.
"""
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile


def main():
    engine = os.environ.get('HOMELAB_TEST_ENGINE', 'podman')
    socket = os.environ.get('HOMELAB_TEST_ENGINE_SOCKET', '/run/podman/podman.sock')
    name = 'homelab-recovery-test-' + secrets.token_hex(5)
    root = Path(__file__).resolve().parents[1]
    def run(*args, **kwargs): return subprocess.run([engine, *args], check=True, **kwargs)
    try:
        run('volume', 'create', name, stdout=subprocess.DEVNULL)
        mount = subprocess.check_output([engine, 'volume', 'inspect', name, '--format', '{{.Mountpoint}}'], text=True).strip()
        run('create', '--name', name, '--security-opt', 'label=disable', '-v', socket+':/var/run/docker.sock', '-v', name+':'+mount,
            '-e', 'DOCKER_API_VERSION=1.41', '-e', 'HOMELAB_RECOVERY_INTEGRATION=1', '-e', 'HOMELAB_RECOVERY_FIXTURE_ROOT='+mount,
            'docker:27-cli', 'sh', '-c', 'apk add --no-cache python3 py3-yaml >/dev/null && python3 -m unittest discover -s /code/tests -p test_recovery_database_integration.py -v', stdout=subprocess.DEVNULL)
        with tempfile.TemporaryDirectory() as temporary:
            code = Path(temporary) / 'code'
            for role in ('backup', 'recovery'):
                shutil.copytree(root/'roles'/role/'files', code/'roles'/role/'files', ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(root/'tests', code/'tests', ignore=shutil.ignore_patterns('__pycache__'))
            run('cp', str(code), name+':/code')
        run('start', '-a', name)
        exitcode = subprocess.check_output([engine, 'inspect', name, '--format', '{{.State.ExitCode}}'], text=True).strip()
        if exitcode != '0': raise SystemExit(1)
    finally:
        # Exact fixture name only. Child fixture cleanup is inside each test.
        subprocess.run([engine, 'rm', '-f', '-v', name], stdout=subprocess.DEVNULL, check=False)
        subprocess.run([engine, 'volume', 'rm', name], stdout=subprocess.DEVNULL, check=False)

if __name__ == '__main__': main()

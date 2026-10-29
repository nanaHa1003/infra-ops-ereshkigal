"""Exercise the real image with synthetic credentials; never contact DNS APIs."""
import re
import os
import subprocess
import sys
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]


def run(image, *args, config=None):
    return subprocess.run(
        [os.environ.get('CONTAINER_ENGINE', 'docker'), 'run', '--rm', '--network', 'none', '-i', '--entrypoint', 'caddy', image, *args],
        input=config, text=True, capture_output=True, check=True, timeout=120,
    ).stdout


def main(image):
    version = re.search(r'ARG CADDY_VERSION=(\S+)', (ROOT / 'images/caddy/Dockerfile').read_text())[1]
    assert run(image, 'version').split()[0] == version, 'unexpected Caddy binary version'
    modules = set(run(image, 'list-modules').splitlines())
    assert {'dns.providers.cloudflare', 'dns.providers.desec'} <= modules
    context = yaml.safe_load((ROOT / 'roles/proxy/defaults/main.yml').read_text())
    context.update(domain='legacy.example.net', vault_secrets={
        'desec': {'api_token': 'validation-only-desec'},
        'cloudflare': {'caddy_api_token': 'A' * 40},
    })
    template = Environment(undefined=StrictUndefined).from_string(
        (ROOT / 'roles/proxy/templates/Caddyfile.j2').read_text())
    for public_domain in ('', 'new.example.net'):
        config = template.render(**{**context, 'proxy_public_domain': public_domain})
        run(image, 'validate', '--config', '/dev/stdin', '--adapter', 'caddyfile', config=config)
        parsed = yaml.safe_load(run(image, 'adapt', '--config', '/dev/stdin', '--adapter', 'caddyfile', config=config))
        policies = parsed['apps']['tls']['automation']['policies']
        providers = {issuer['challenges']['dns']['provider']['name']
                     for policy in policies for issuer in policy['issuers']}
        assert providers == ({'desec', 'cloudflare'} if public_domain else {'desec'})
    print('Caddy version, both DNS providers, and legacy/parallel configurations verified.')


if __name__ == '__main__':
    try:
        main(sys.argv[1])
    except subprocess.CalledProcessError as exc:
        # All input is synthetic and image validation has no network access.
        print(exc.stderr, file=sys.stderr)
        raise

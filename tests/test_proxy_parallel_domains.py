"""Domain migration must retain old entry points and isolate ACME providers."""
from pathlib import Path
import unittest
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]


class ParallelDomains(unittest.TestCase):
    def render(self, **extra):
        context = {'domain': 'ereshkigal.dedyn.io',
                   'vault_secrets': {'desec': {'api_token': 'fake-desec-token'},
                                     'cloudflare': {'caddy_api_token': 'A' * 40}}}
        context.update(extra)
        return Environment(undefined=StrictUndefined).from_string(
            (ROOT / 'roles/proxy/templates/Caddyfile.j2').read_text()).render(**context)

    def test_new_domain_keeps_old_routes_and_uses_separate_tls(self):
        config = self.render(proxy_public_domain='ereshkigal.cc')
        for prefix in ('auth.', 'blog.', 'cook.', 'tube.', 'photo.', 'auto.'):
            self.assertIn(prefix + 'ereshkigal.dedyn.io {', config)
            self.assertIn(prefix + 'ereshkigal.cc {', config)
        self.assertIn('ereshkigal.dedyn.io {', config)
        self.assertIn('www.ereshkigal.cc {', config)
        self.assertNotIn('\nereshkigal.cc {', config)
        self.assertEqual(config.count('import desec_tls'), 7)
        self.assertEqual(config.count('import public_tls'), 7)
        self.assertIn('dns cloudflare ' + 'A' * 40, config)
        self.assertNotIn('disable_tlsalpn_challenge', config)

    def test_unconfigured_public_domain_preserves_legacy_only(self):
        config = self.render()
        self.assertNotIn('ereshkigal.cc', config)
        self.assertNotIn('import public_tls', config)


if __name__ == '__main__':
    unittest.main()

"""Render the SMTP configuration consumed by both Authentik processes."""

from pathlib import Path
import unittest

from jinja2 import Environment, StrictUndefined
import yaml

ROOT = Path(__file__).resolve().parents[1]


class AuthentikSMTP(unittest.TestCase):
    def test_server_and_worker_receive_configured_smtp(self):
        context = yaml.safe_load((ROOT / "roles/auth/defaults/main.yml").read_text())
        context.update({
            "storage_root": "/test/storage",
            "vault_secrets": yaml.safe_load(
                (ROOT / "inventory/group_vars/all/vault.ci.yml").read_text()
            )["vault_secrets"],
            "auth_smtp_host": "smtp.example.test",
            "auth_smtp_port": 587,
            "auth_smtp_use_tls": "True",
            "auth_smtp_use_ssl": "False",
            "auth_smtp_timeout": 25,
            "auth_email_from": "auth@example.test",
        })
        context["vault_secrets"]["authentik"].update({
            "email_username": "test-smtp-user",
            "email_password": "test-smtp-password",
        })
        source = (ROOT / "roles/auth/templates/auth.yml.j2").read_text()
        services = yaml.safe_load(
            Environment(undefined=StrictUndefined).from_string(source).render(context)
        )["services"]
        expected = {
            "AUTHENTIK_EMAIL__HOST": "smtp.example.test",
            "AUTHENTIK_EMAIL__PORT": "587",
            "AUTHENTIK_EMAIL__FROM": "auth@example.test",
            "AUTHENTIK_EMAIL__USERNAME": "test-smtp-user",
            "AUTHENTIK_EMAIL__PASSWORD": "test-smtp-password",
            "AUTHENTIK_EMAIL__USE_TLS": "True",
            "AUTHENTIK_EMAIL__USE_SSL": "False",
            "AUTHENTIK_EMAIL__TIMEOUT": "25",
        }
        for name in ("authentik-server", "authentik-worker"):
            with self.subTest(service=name):
                actual = services[name]["environment"]
                self.assertEqual({k: actual.get(k) for k in expected}, expected)


if __name__ == "__main__":
    unittest.main()

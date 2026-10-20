"""Contracts for the Authentik 2026.8 upgrade and trusted proxy setup."""

from ipaddress import ip_interface, ip_network
from pathlib import Path
import unittest

from jinja2 import Environment, StrictUndefined
import yaml

ROOT = Path(__file__).resolve().parents[1]


def read_yaml(path):
    return yaml.safe_load((ROOT / path).read_text())


def render(path, context):
    template = (ROOT / path).read_text()
    return yaml.safe_load(Environment(undefined=StrictUndefined).from_string(template).render(context))


class AuthentikUpgradeContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.host = read_yaml("inventory/host_vars/ereshkigal.netbird.cloud/vars.yml")
        cls.auth_defaults = read_yaml("roles/auth/defaults/main.yml")
        cls.proxy_defaults = read_yaml("roles/proxy/defaults/main.yml")

    def test_authentik_server_and_worker_use_the_pinned_target_release(self):
        self.assertEqual(self.auth_defaults["auth_authentik_image"],
                         "ghcr.io/goauthentik/server:2026.8.4")

    def test_recovery_accepts_current_and_previous_authentik_image_families(self):
        services = read_yaml("roles/recovery/vars/services.yml")
        pattern = services["recovery_service_registry"]["auth"]["verification"]["image_pattern"]
        self.assertRegex("ghcr.io/goauthentik/server:2026.5.7", pattern)
        self.assertRegex("ghcr.io/goauthentik/server:2026.8.4", pattern)
        self.assertNotRegex("ghcr.io/goauthentik/server:2026.6.0", pattern)

    def test_proxy_uses_the_host_pinned_address(self):
        self.assertTrue(self.host.get("proxy_caddy_ipv4_address"),
                        "the host must pin the address used by Caddy")
        context = {**self.proxy_defaults, **self.host,
                   "stack_root": "/test/stack", "storage_root": "/test/storage"}
        compose = render("roles/proxy/templates/proxy.yml.j2", context)
        networks = compose["services"]["caddy"]["networks"]
        network = networks.get("backend") if isinstance(networks, dict) else next(
            (value for value in networks if isinstance(value, dict) and "backend" in value), None
        )
        self.assertIsInstance(network, dict, "Caddy backend must declare its pinned IP")
        address = network.get("ipv4_address")
        interface = ip_interface(address)
        self.assertEqual(str(interface.ip), self.host["proxy_caddy_ipv4_address"])

    def test_authentik_trusts_only_the_caddy_address(self):
        self.assertTrue(self.host.get("auth_trusted_proxy_cidrs"),
                        "the host must define Authentik's trusted proxy CIDRs")
        context = {
            **self.auth_defaults,
            "storage_root": "/test/storage",
            "vault_secrets": read_yaml("inventory/group_vars/all/vault.ci.yml")["vault_secrets"],
            "auth_trusted_proxy_cidrs": self.host.get("auth_trusted_proxy_cidrs", ""),
        }
        services = render("roles/auth/templates/auth.yml.j2", context)["services"]
        raw_cidrs = services["authentik-server"]["environment"][
            "AUTHENTIK_LISTEN__TRUSTED_PROXY_CIDRS"
        ]
        trusted = [ip_network(value.strip()) for value in raw_cidrs.split(",")]
        caddy_ip = ip_interface(self.host["proxy_caddy_ipv4_address"]).ip
        self.assertEqual(trusted, [ip_network(f"{caddy_ip}/32")])
        self.assertEqual(services["authentik-worker"]["environment"],
                         services["authentik-server"]["environment"])


if __name__ == "__main__":
    unittest.main()

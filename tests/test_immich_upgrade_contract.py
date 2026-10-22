"""Contracts for the Immich v3 upgrade on the current CPU-only host."""

from pathlib import Path
import unittest

from jinja2 import Environment, StrictUndefined
import yaml


ROOT = Path(__file__).resolve().parents[1]


def read_yaml(path):
    return yaml.safe_load((ROOT / path).read_text())


def render_immich(context):
    source = (ROOT / "roles/immich/templates/immich.yml.j2").read_text()
    return yaml.safe_load(
        Environment(undefined=StrictUndefined).from_string(source).render(context)
    )


class ImmichUpgradeContract(unittest.TestCase):
    def test_server_and_machine_learning_images_share_the_target_release(self):
        defaults = read_yaml("roles/immich/defaults/main.yml")
        self.assertEqual(
            defaults["immich_server_image"],
            "ghcr.io/immich-app/immich-server:v3.3.3",
        )
        self.assertEqual(
            defaults["immich_ml_image"],
            "ghcr.io/immich-app/immich-machine-learning:v3.3.3",
        )

    def test_database_image_and_cpu_inference_defaults_are_current(self):
        defaults = read_yaml("roles/immich/defaults/main.yml")
        self.assertEqual(
            defaults["immich_postgres_image"],
            "ghcr.io/immich-app/postgres:18-vectorchord1.1.1-pgvector0.8.5",
        )
        self.assertEqual(defaults["immich_postgres_data_path"], "immich/postgres-pg18")
        self.assertEqual(defaults["immich_postgres_mount_path"], "/var/lib/postgresql")
        self.assertEqual(defaults["immich_ml_devices"], [])
        self.assertEqual(defaults["immich_ml_usb_bus"], "")
        self.assertNotIn("-openvino", defaults["immich_ml_image"])

    def test_postgres_template_uses_the_live_pg18_volume_layout(self):
        defaults = read_yaml("roles/immich/defaults/main.yml")
        context = {
            **defaults,
            "storage_root": "/mnt/storage",
            "vault_secrets": {
                "immich": {
                    "db_database_name": "immich",
                    "db_username": "immich",
                    "db_password": "test-only",
                }
            },
        }
        compose = render_immich(context)
        self.assertEqual(
            compose["services"]["database"]["volumes"],
            ["/mnt/storage/immich/postgres-pg18:/var/lib/postgresql:Z"],
        )

    def test_storage_permissions_cover_live_pg18_and_preserved_pg14_directories(self):
        variables = read_yaml("inventory/group_vars/homelab/vars.yml")
        permissions = {row["path"]: row for row in variables["storage_permissions"]}
        for path in ("immich/postgres", "immich/postgres-pg18"):
            self.assertEqual(permissions[path]["owner"], 999)
            self.assertEqual(permissions[path]["group"], 0)
            self.assertEqual(permissions[path]["mode"], "0700")


if __name__ == "__main__":
    unittest.main()

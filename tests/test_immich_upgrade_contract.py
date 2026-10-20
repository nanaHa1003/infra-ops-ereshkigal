"""Contracts for the Immich v3 upgrade on the current CPU-only host."""

from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


def read_yaml(path):
    return yaml.safe_load((ROOT / path).read_text())


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

    def test_database_image_and_cpu_inference_defaults_are_preserved(self):
        defaults = read_yaml("roles/immich/defaults/main.yml")
        self.assertEqual(
            defaults["immich_postgres_image"],
            "ghcr.io/immich-app/postgres:14-vectorchord0.4.3-pgvectors0.2.0",
        )
        self.assertEqual(defaults["immich_ml_devices"], [])
        self.assertEqual(defaults["immich_ml_usb_bus"], "")
        self.assertNotIn("-openvino", defaults["immich_ml_image"])


if __name__ == "__main__":
    unittest.main()

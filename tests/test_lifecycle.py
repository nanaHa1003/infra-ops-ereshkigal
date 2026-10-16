"""Regression checks for the public deployment and security contracts."""

from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from ansible.plugins.filter.core import to_bool
from jinja2 import Environment, StrictUndefined
import yaml


ROOT = Path(__file__).resolve().parents[1]
ACTIVE = ("proxy", "auth", "wordpress", "pinchflat", "immich", "mealie", "homepage", "n8n", "aria2")


def read_yaml(path):
    return yaml.safe_load((ROOT / path).read_text())


def render(value, context):
    env = Environment(undefined=StrictUndefined)
    env.filters["bool"] = to_bool
    return env.from_string(str(value)).render(context)


class LifecycleContracts(unittest.TestCase):
    def test_aria2_image_receives_the_vault_token_as_SECRET(self):
        context = read_yaml("roles/aria2/defaults/main.yml")
        context.update({"storage_root": "/mnt/storage", "timezone": "UTC",
                        "vault_secrets": {"aria2": {"rpc_secret": "test-token"}}})
        source = (ROOT / "roles/aria2/templates/aria2.yml.j2").read_text()
        service = yaml.safe_load(render(source, context))["services"]["aria2"]
        environment = dict(item.split("=", 1) for item in service["environment"])
        # superng6/aria2's run script passes SECRET as --rpc-secret.
        self.assertEqual(environment.get("SECRET"), "test-token")

    def normalize_proxy_task(self, parameters):
        with tempfile.TemporaryDirectory(prefix="homelab-proxy-") as directory:
            context = read_yaml("roles/proxy/defaults/main.yml")
            context.update({"stack_root": directory, "storage_root": "/mnt/storage", "timezone": "UTC"})
            for source, target in (("proxy.yml.j2", "proxy"), ("docker-socket.yml.j2", "docker-proxy")):
                destination = Path(directory) / target / "docker-compose.yml"
                destination.parent.mkdir()
                destination.write_text(render((ROOT / "roles/proxy/templates" / source).read_text(), context))
            project = render(parameters["project_src"], context)
            files = parameters.get("files", ["docker-compose.yml"])
            if isinstance(files, str):
                files = yaml.safe_load(render(files, {**context,
                    "proxy_caddy_compose_file": {"stat": {"exists": True}},
                    "proxy_socket_compose_file": {"stat": {"exists": True}},
                }))
            standalone = shutil.which("docker-compose")
            command = [standalone] if standalone else ["docker", "compose"]
            command += ["--project-name", parameters.get("project_name", "proxy")]
            for file in files:
                command += ["-f", str(Path(project) / render(file, context))]
            result = subprocess.run(command + ["config", "--format", "json"], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_proxy_operations_use_the_complete_existing_project(self):
        for file in ("main.yml", "down.yml"):
            for task in read_yaml(f"roles/proxy/tasks/{file}"):
                parameters = task.get("community.docker.docker_compose_v2")
                if parameters:
                    with self.subTest(file=file, task=task["name"]):
                        model = self.normalize_proxy_task(parameters)
                        self.assertEqual(set(model["services"]), {"caddy", "docker-ro", "docker-rw"})

    def test_proxy_shared_networks_are_external(self):
        for task in read_yaml("roles/proxy/tasks/main.yml"):
            parameters = task.get("community.docker.docker_compose_v2")
            if parameters:
                model = self.normalize_proxy_task(parameters)
                for network in model["networks"].values():
                    with self.subTest(task=task["name"], network=network["name"]):
                        self.assertTrue(network.get("external", False))

    def test_proxy_recreation_is_scoped_to_requested_services(self):
        expected = {
            "Start/Update Caddy reverse proxy": ["caddy"],
            "Start/Update Docker socket proxies": ["docker-ro", "docker-rw"],
        }
        for task in read_yaml("roles/proxy/tasks/main.yml"):
            parameters = task.get("community.docker.docker_compose_v2")
            if parameters:
                with self.subTest(task=task["name"]):
                    self.assertEqual(parameters.get("services"), expected[task["name"]])
                    self.assertIs(parameters.get("dependencies"), False)

    def render_immich(self, host_vars=None):
        context = read_yaml("roles/immich/defaults/main.yml")
        context.update({
            "storage_root": "/mnt/storage",
            "vault_secrets": read_yaml("inventory/group_vars/all/vault.ci.yml")["vault_secrets"],
        })
        context.update(host_vars or {})
        source = (ROOT / "roles/immich/templates/immich.yml.j2").read_text()
        return yaml.safe_load(render(source, context))["services"]["immich-machine-learning"]

    def test_current_host_uses_cpu_ml_without_hardware_access(self):
        ml = self.render_immich(read_yaml("inventory/host_vars/ereshkigal.netbird.cloud/vars.yml"))
        self.assertNotIn("-openvino", ml["image"])
        self.assertNotIn("devices", ml)
        self.assertNotIn("device_cgroup_rules", ml)
        self.assertEqual(ml["volumes"], ["model-cache:/cache"])

    def test_default_ml_compose_has_no_empty_hardware_mounts(self):
        ml = self.render_immich()
        self.assertNotIn("devices", ml)
        self.assertNotIn("device_cgroup_rules", ml)
        self.assertEqual(ml["volumes"], ["model-cache:/cache"])

    def test_explicit_hardware_overrides_still_render(self):
        ml = self.render_immich({
            "immich_ml_image": "ghcr.io/immich-app/immich-machine-learning:v2.7.5-openvino",
            "immich_ml_devices": ["/dev/dri:/dev/dri"],
            "immich_ml_usb_bus": "/dev/bus/usb",
        })
        self.assertEqual(ml["devices"], ["/dev/dri:/dev/dri"])
        self.assertIn("c 189:* rmw", ml["device_cgroup_rules"])
        self.assertIn("/dev/bus/usb:/dev/bus/usb", ml["volumes"])

    def test_named_model_cache_has_no_bind_options(self):
        ml = self.render_immich()
        compose = {
            "services": {"ml": ml},
            "networks": {"backend": {}},
            "volumes": {"model-cache": {}},
        }
        standalone = shutil.which("docker-compose")
        command = [standalone] if standalone else ["docker", "compose"]
        result = subprocess.run(
            command + ["--project-name", "homelab-test", "-f", "-", "config", "--format", "json"],
            input=yaml.safe_dump(compose), capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        mount = next(v for v in json.loads(result.stdout)["services"]["ml"]["volumes"] if v["target"] == "/cache")
        self.assertEqual(mount["type"], "volume")
        self.assertEqual(mount["source"], "model-cache")
        self.assertNotIn("bind", mount)

    def test_unchanged_templates_still_reconcile_services(self):
        for role in ACTIVE:
            tasks = read_yaml(f"roles/{role}/tasks/main.yml")
            context = {task["register"]: {"changed": False} for task in tasks if "register" in task}
            context[f"{role}_force_restart"] = False
            for task in tasks:
                if "community.docker.docker_compose_v2" not in task:
                    continue
                with self.subTest(role=role, task=task["name"]):
                    condition = task.get("when", "true")
                    self.assertEqual(render("{{ (" + condition + ") | bool }}", context), "True")

    def test_force_restart_controls_recreation_including_string_false(self):
        for role in ACTIVE:
            tasks = read_yaml(f"roles/{role}/tasks/main.yml")
            defaults = read_yaml(f"roles/{role}/defaults/main.yml")
            for flag, expected in ((False, "auto"), ("false", "auto"), (True, "always"), ("true", "always")):
                context = {task["register"]: {"changed": False} for task in tasks if "register" in task}
                context[f"{role}_force_restart"] = render(defaults[f"{role}_force_restart"], {"force_restart": flag})
                for task in tasks:
                    if "community.docker.docker_compose_v2" in task:
                        with self.subTest(role=role, flag=flag):
                            config = task["community.docker.docker_compose_v2"]
                            self.assertEqual(render(config.get("recreate", "auto"), context), expected)

    def test_caddy_config_change_recreates_the_bound_file(self):
        tasks = read_yaml("roles/proxy/tasks/main.yml")
        task = next(task for task in tasks if task["name"] == "Start/Update Caddy reverse proxy")
        context = {"proxy_caddy_config": {"changed": True}, "proxy_force_restart": False}
        config = task["community.docker.docker_compose_v2"]
        self.assertEqual(render(config.get("recreate", "auto"), context), "always")

    def test_rendered_secrets_are_private_and_excluded_from_diffs(self):
        for path in (ROOT / "roles").glob("*/tasks/main.yml"):
            for task in yaml.safe_load(path.read_text()):
                template = task.get("ansible.builtin.template")
                if not template:
                    continue
                for item in task.get("loop", [None]):
                    source = path.parent.parent / "templates" / render(template["src"], {"item": item})
                    if "vault_secrets" not in source.read_text():
                        continue
                    with self.subTest(role=path.parent.parent.name, source=source.name):
                        self.assertEqual(int(template["mode"], 8) & 0o077, 0)
                        self.assertIs(task.get("diff"), False)

    def test_shared_paths_have_shared_labels_and_proxy_is_read_only(self):
        context = {"stack_root": "/opt/stack", "storage_root": "/mnt/storage", "proxy_caddy_image": "caddy:test"}
        doc = yaml.safe_load(render((ROOT / "roles/proxy/templates/proxy.yml.j2").read_text(), context))
        mount = next(v for v in doc["services"]["caddy"]["volumes"] if "/apps/wordpress:" in v)
        self.assertEqual(set(mount.rsplit(":", 1)[1].split(",")), {"ro", "z"})

    def test_role_tags_are_available_for_every_active_service(self):
        play = read_yaml("site.yml")[0]
        tagged = {role["role"]: role.get("tags", []) for role in play["roles"] if isinstance(role, dict)}
        for role in ACTIVE:
            with self.subTest(role=role):
                self.assertIn(role, tagged.get(role, []))


class AnsibleSelection(unittest.TestCase):
    """Exercise selection through Ansible itself, with a local dummy inventory."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="homelab-selection-")
        self.addCleanup(self.temp.cleanup)
        self.scratch = Path(self.temp.name)
        self.inventory = self.scratch / "hosts.ini"
        self.inventory.write_text("[homelab]\nlocalhost ansible_connection=local\n")
        self.config = self.scratch / "ansible.cfg"
        self.config.write_text(
            f"[defaults]\nroles_path={ROOT / 'roles'}\n"
            f"local_tmp={self.scratch / 'local-tmp'}\n"
            f"remote_tmp={self.scratch / 'remote-tmp'}\n"
            "[privilege_escalation]\nbecome_ask_pass=false\n"
        )

    def run_playbook(self, playbook, *args, enabled=None):
        executable = shutil.which("ansible-playbook") or str(Path(sys.executable).parent / "ansible-playbook")
        env = dict(os.environ, ANSIBLE_CONFIG=str(self.config), ANSIBLE_NOCOLOR="1")
        env.pop("ANSIBLE_VAULT_PASSWORD_FILE", None)
        variables = {
            "ansible_become": False,
            "ansible_python_interpreter": sys.executable,
            "stack_root": str(self.scratch / "stacks"),
            "enabled_services": list(ACTIVE) if enabled is None else enabled,
        }
        return subprocess.run(
            [executable, str(ROOT / playbook), "-i", str(self.inventory),
             "-e", json.dumps(variables), *args],
            env=env, cwd=self.scratch, capture_output=True, text=True, timeout=60,
        )

    def test_tagged_deploy_selects_the_service_without_other_stacks(self):
        result = self.run_playbook("site.yml", "--list-tasks", "--tags", "n8n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Start/Update n8n", result.stdout)
        self.assertNotIn("Start/Update Caddy", result.stdout)
        self.assertNotIn("Start/Update Authentik", result.stdout)

    def test_teardown_never_selects_deployment_tasks(self):
        result = self.run_playbook("down.yml", "--list-tasks")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Shut down Mealie", result.stdout)
        self.assertNotIn("Start/Update", result.stdout)
        self.assertNotIn("Deploy ", result.stdout)

    def test_disabled_service_is_skipped_even_when_tagged(self):
        result = self.run_playbook("site.yml", "--check", "--tags", "n8n", enabled=[])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("skipping: [localhost]", result.stdout)
        self.assertRegex(result.stdout, r"changed=0\s")
        self.assertFalse((self.scratch / "stacks").exists())


if __name__ == "__main__":
    unittest.main()

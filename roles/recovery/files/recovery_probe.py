"""Read-only target facts. Executed before any recovery program installation."""
import json
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys

config = json.load(sys.stdin)
storage = Path(config["storage_root"])
mount = Path(config["storage_mount"])
nonempty, symlinks = [], []
for service in config["services"]:
    for definition in config["registry"][service]["sources"].values():
        if definition["layout"] == "reference": continue
        relative = definition["target"]
        root = definition.get("root", relative if definition["layout"] in ("directory", "database") else str(PurePosixPath(relative).parent))
        path = storage / root
        if any(p.is_symlink() for p in [path, *path.parents]): symlinks.append(str(path))
        elif path.exists() and (not path.is_dir() or any(path.iterdir())): nonempty.append(str(path))

stack = Path(config["stack_root"])
if any(p.is_symlink() for p in [stack, *stack.parents]): symlinks.append(str(stack))
if stack.exists() and (not stack.is_dir() or any(stack.iterdir())): nonempty.append(str(stack))

service_probe_failed = False

def selected_output(args):
    global service_probe_failed
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=20, check=False)
        if result.returncode != 0:
            service_probe_failed = True
            return ""
        return result.stdout
    except (OSError, subprocess.TimeoutExpired):
        service_probe_failed = True
        return ""

containers = []
if shutil.which("docker"):
    for service in config["services"]:
        containers.extend(selected_output(["docker", "--host", "unix:///var/run/docker.sock", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + service]).split())
timers = selected_output(["systemctl", "list-units", "--type=timer", "--state=active", "--no-legend", "--plain", "homelab-backup*"])
cron = Path("/var/spool/cron/root")
cron_text = cron.read_text() if cron.exists() else ""
machine = Path("/etc/machine-id")
print(json.dumps({"mount_ok": mount.is_mount() and not symlinks,
    "free_bytes": shutil.disk_usage(mount).free if mount.exists() else 0,
    "machine_id": machine.read_text().strip() if machine.exists() else None,
    "nonempty_roots": sorted(set(nonempty + symlinks)), "conflicting_services": containers,
    "active_backup_timers": bool(timers.strip()), "ddns_active": "ddupdate.sh" in cron_text,
    "service_probe_failed": service_probe_failed, "restic": bool(shutil.which("restic")), "docker": bool(shutil.which("docker"))}))

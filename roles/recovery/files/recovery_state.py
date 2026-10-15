"""Atomic recovery progress, host-wide exclusion and bounded cleanup ownership."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import secrets

from backup_common import BackupError, checked_path, safe_id

PHASES = ("planned", "downloaded", "prepared", "imported", "verified", "activated")


def save_state(state):
    destination = checked_path(state["_state_file"])
    temporary = destination.parent / (".state-" + secrets.token_hex(8))
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump({key: value for key, value in state.items() if not key.startswith("_")}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise BackupError("recovery state could not be saved atomically") from exc
    finally:
        if temporary.exists(): temporary.unlink()


def open_run(plan: dict, state_root: Path) -> dict:
    root = checked_path(str(state_root))
    run_id = safe_id(plan["run_id"])
    directory = checked_path(str(root / "runs" / run_id))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = checked_path(str(directory / "state.json"))
    identity = {"run_id": run_id, "target_machine_id": plan["target"]["machine_id"],
                "snapshot_id": plan["selection"]["snapshot_id"], "profile_sha256": plan["profile_sha256"]}
    if path.exists():
        if path.stat().st_mode & 0o077:
            raise BackupError("recovery state permissions are not protected")
        state = json.loads(path.read_text())
        if any(state.get(key) != value for key, value in identity.items()):
            raise BackupError("recovery run target snapshot or profile changed")
        if sorted(state["services"]) != sorted(plan["services"]):
            raise BackupError("recovery run services changed")
    else:
        state = {**identity, "phase": "planned", "services": {name: {"phase": "planned"} for name in plan["services"]},
                 "owned_paths": [], "owned_containers": [], "results": {},
                 "activation": {"services_started": [], "cutover_confirmed": False, "ddns_enabled": False}}
    state["_state_file"] = str(path)
    state["_run_root"] = str(directory)
    state["_approved_roots"] = [str(checked_path(value)) for value in plan.get("approved_roots", [])]
    save_state(state)
    return state


def advance_service(state: dict, service: str, phase: str, result: dict) -> None:
    safe_id(service)
    if service not in state["services"] or phase not in PHASES:
        raise BackupError("invalid recovery service transition")
    previous = PHASES.index(state["services"][service]["phase"])
    index = PHASES.index(phase)
    if index not in (previous, previous + 1):
        raise BackupError("recovery state transition is not sequential")
    state["services"][service]["phase"] = phase
    state["results"][service] = result
    state["phase"] = min((entry["phase"] for entry in state["services"].values()), key=PHASES.index)
    save_state(state)


@contextmanager
def run_lock(state_root):
    root = checked_path(str(state_root))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    lock_path = checked_path(str(root / "recovery.lock"))
    with lock_path.open("a") as lock:
        lock_path.chmod(0o600)
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("another recovery operation is running") from exc
        yield


def cleanup_preview(state: dict) -> list:
    if state.get("phase") == "activated" or state.get("activation", {}).get("services_started"):
        raise BackupError("cleanup refused after application activation")
    approved = set(state.get("_approved_roots", []))
    preview = []
    for value in state.get("owned_paths", []):
        path = checked_path(value)
        if str(path) not in approved:
            raise BackupError("cleanup path is not an approved run-owned service root")
        preview.append({"kind": "path", "value": str(path)})
    for name in state.get("owned_containers", []):
        safe_id(name)
        if not name.startswith("recovery-") or state["run_id"] not in name:
            raise BackupError("cleanup container is not a recovery resource")
        preview.append({"kind": "container", "value": name})
    for name in state.get("owned_networks", []):
        if name != "recovery-" + state["run_id"]: raise BackupError("cleanup network is not a recovery resource")
        preview.append({"kind": "network", "value": name})
    return sorted(preview, key=lambda item: {"container": 0, "network": 1, "path": 2}[item["kind"]])

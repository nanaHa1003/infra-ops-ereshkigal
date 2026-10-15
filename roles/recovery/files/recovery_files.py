"""Map snapshot artifacts onto explicitly approved, empty service destinations."""
import hashlib
import os
from pathlib import Path, PurePosixPath
import re

from backup_common import BackupError, checked_path, safe_id
from backup_sources import command, copy_files, stable_copy
from recovery_manifest import read_compose_fields
from recovery_models import relative_artifact
from recovery_state import save_state


def build_mappings(model: dict, target: dict, registry: dict) -> list:
    storage = checked_path(target["storage_root"])
    scratch = checked_path(target["scratch_root"]) if target.get("scratch_root") else None
    mappings = []
    for service in target["services"]:
        safe_id(service)
        if service not in registry:
            raise BackupError("unsupported recovery service")
        for identifier, definition in registry[service]["sources"].items():
            if definition.get("layout") == "reference":
                continue
            source = model["sources"].get(identifier)
            if not source or source.get("optional_absent"):
                if definition.get("required", True):
                    raise BackupError("required recovery source unavailable")
                continue
            if source["kind"] != definition["kind"]:
                raise BackupError("source kind does not match service registry")
            relative = relative_artifact(definition["target"])
            destination = checked_path(str(storage / relative))
            root = checked_path(str(storage / relative_artifact(definition.get("root", relative if definition["layout"] == "directory" or source["kind"] in ("postgres", "mariadb") else str(PurePosixPath(relative).parent)))))
            if scratch and (scratch == root or scratch in root.parents or root in scratch.parents):
                raise BackupError("scratch overlaps recovery destination")
            artifacts = source["artifacts"]
            if definition["layout"] == "file" or source["kind"] in ("postgres", "mariadb", "sqlite", "aria2_session"):
                artifacts = [item for item in artifacts if item["entry_type"] == "file"]
                if len(artifacts) != 1:
                    raise BackupError("single-file recovery source is ambiguous")
            for artifact in artifacts:
                path = PurePosixPath(relative_artifact(artifact["relative_path"]))
                if path.parts[0] != identifier:
                    raise BackupError("artifact belongs to another source")
                suffix = Path(*path.parts[1:])
                output = destination / suffix if definition["layout"] == "directory" else destination
                output = checked_path(str(output))
                if output != root and root not in output.parents:
                    raise BackupError("artifact escapes service root")
                mappings.append({"source_id": identifier, "kind": source["kind"], "artifact": artifact,
                    "tree_root": model["selection"]["tree_root"], "target": str(output), "service_root": str(root),
                    "owner": definition.get("owner", 1000), "group": definition.get("group", 1000),
                    "mode": definition.get("mode", "0600"), "service": service,
                    "mount": target["storage_mount"], "run_id": target.get("run_id"),
                    "legacy": model.get("schema_version") == 1,
                    "core_image": target.get("wordpress_core_image") if service == "wordpress" else None})
    for index, first in enumerate(mappings):
        a = Path(first["target"])
        for second in mappings[index + 1:]:
            b = Path(second["target"])
            if first["source_id"] != second["source_id"] and (a == b or a in b.parents or b in a.parents):
                raise BackupError("recovery source destinations overlap")
    return mappings


def _owned(row, state):
    if not state or not row.get("run_id") or state.get("run_id") != row["run_id"]:
        return False
    root = Path(row["service_root"])
    for value in state.get("owned_paths", []):
        candidate = checked_path(value)
        if candidate == root:
            return True
    return False


def validate_targets(mappings: list, state: dict | None) -> None:
    for row in mappings:
        mount = checked_path(row["mount"])
        root = checked_path(row["service_root"])
        checked_path(row["target"])
        if not mount.is_mount() or (root != mount and mount not in root.parents):
            raise BackupError("required storage filesystem is not mounted")
        if root.exists() and (not root.is_dir() or any(root.iterdir())) and not _owned(row, state):
            raise BackupError("recovery destination is not empty or owned by this run")


def _artifact_path(row, scratch):
    base = checked_path(str(scratch))
    tree = PurePosixPath(row["tree_root"])
    if not tree.is_absolute() or ".." in tree.parts:
        raise BackupError("invalid restored tree root")
    return checked_path(str(base.joinpath(*tree.parts[1:]) / relative_artifact(row["artifact"]["relative_path"])))


def _metadata(path, row, directory=False):
    checked_path(str(path))
    path.chmod(0o700 if directory else int(row["mode"], 8))
    if (path.stat().st_uid, path.stat().st_gid) != (row["owner"], row["group"]):
        if os.geteuid() != 0:
            raise BackupError("root required to apply service ownership")
        os.chown(path, row["owner"], row["group"])


def prepare_wordpress_core(image, destination, state):
    if not isinstance(image, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:-]*@sha256:[a-f0-9]{64}", image):
        raise BackupError("pinned WordPress core image required")
    name = "recovery-core-" + safe_id(state["run_id"])
    state.setdefault("owned_containers", []).append(name)
    save_state(state)
    command(["docker", "create", "--name", name, "--label", "homelab.recovery.run=" + state["run_id"], "--network", "none", image], timeout=120)
    try:
        command(["docker", "cp", name + ":/usr/src/wordpress/.", str(destination)], timeout=120)
    finally:
        command(["docker", "rm", "-v", name], timeout=30)
        state["owned_containers"].remove(name)
        save_state(state)


def validate_artifacts(mappings: list, scratch: Path) -> None:
    for row in mappings:
        source = _artifact_path(row, scratch)
        artifact = row["artifact"]
        if not source.exists() or (artifact["entry_type"] == "directory") != source.is_dir():
            raise BackupError("recovery artifact missing or wrong type")
        if source.is_dir() and row.get("legacy"):
            for descendant in source.rglob("*"):
                checked_path(str(descendant))
                if not descendant.is_file() and not descendant.is_dir():
                    raise BackupError("unsupported legacy artifact")
        if artifact["entry_type"] == "file" and artifact.get("sha256"):
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != artifact["sha256"] or source.stat().st_size != artifact["size"]:
                raise BackupError("recovery artifact verification failed")


def install_artifacts(mappings: list, scratch: Path, state: dict) -> list:
    validate_targets(mappings, state)
    validate_artifacts(mappings, scratch)
    roots = {}
    for row in mappings:
        if row["kind"] in ("postgres", "mariadb"):
            continue
        root = Path(row["service_root"])
        if str(root) not in roots:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            _metadata(root, row, True)
            if str(root) not in state.setdefault("owned_paths", []):
                state["owned_paths"].append(str(root))
            roots[str(root)] = row
            if row["service"] == "wordpress" and root.name == "html":
                prepare_wordpress_core(row["core_image"], root, state)
                for path in [root, *root.rglob("*")]:
                    _metadata(path, row, path.is_dir())
    for row in mappings:
        if row["kind"] in ("postgres", "mariadb"):
            continue
        source, output = _artifact_path(row, scratch), Path(row["target"])
        if source.is_dir():
            output.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Legacy directory artifacts have no per-file hash; restic already verified them.
            if row.get("legacy"):
                copy_files(source, output, [])
                for path in output.rglob("*"):
                    _metadata(path, row, path.is_dir())
            _metadata(output, row, True)
        else:
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            stable_copy(source, output)
            _metadata(output, row)
    return list(roots)


SECRET_FIELDS = {
    "auth": [("auth-compose", "postgresql", "POSTGRES_PASSWORD", "authentik", "pgpass"),
             ("auth-compose", "authentik-server", "AUTHENTIK_SECRET_KEY", "authentik", "secret_key")],
    "n8n": [("n8n-compose", "n8n-postgres", "POSTGRES_PASSWORD", "n8n", "postgres_password"),
            ("n8n-compose", "n8n-postgres", "POSTGRES_USER", "n8n", "postgres_user"),
            ("n8n-compose", "n8n-postgres", "POSTGRES_DB", "n8n", "postgres_db")],
    "wordpress": [("wordpress-compose", "mariadb", "MYSQL_PASSWORD", "wordpress", "mysql_password"),
                  ("wordpress-compose", "mariadb", "MYSQL_USER", "wordpress", "mysql_user"),
                  ("wordpress-compose", "mariadb", "MYSQL_DATABASE", "wordpress", "mysql_database")],
    "immich": [("immich-compose", "database", "POSTGRES_PASSWORD", "immich", "db_password"),
               ("immich-compose", "database", "POSTGRES_USER", "immich", "db_username"),
               ("immich-compose", "database", "POSTGRES_DB", "immich", "db_database_name")],
    "aria2": [("aria2-compose", "aria2", "SECRET", "aria2", "rpc_secret")],
}


def compare_recovery_secrets(model: dict, scratch: Path, secrets: dict) -> list:
    results = []
    parsed = {}
    for service in model.get("services", []):
        for source_id, container, variable, section, key in SECRET_FIELDS.get(service, []):
            if source_id not in parsed:
                row = {"tree_root": model["selection"]["tree_root"], "artifact": {"relative_path": source_id + "/docker-compose.yml"}}
                path = _artifact_path(row, scratch)
                parsed[source_id] = read_compose_fields(path.read_text()) if path.is_file() else {}
            expected = secrets.get(section, {}).get(key)
            actual = parsed[source_id].get(container, {}).get("environment", {}).get(variable)
            results.append({"service": service, "field": section + "." + key,
                            "matched": isinstance(expected, str) and bool(expected) and actual == expected})
    return results

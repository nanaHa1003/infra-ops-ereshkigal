"""Versioned, non-secret metadata describing the encrypted recovery artifacts."""
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from backup_common import BackupError, checked_path, safe_id
from backup_sources import command


def capture_image_metadata(container):
    safe_id(container)
    selected = '{"image":{{json .Config.Image}},"image_id":{{json .Image}},"service":{{json (index .Config.Labels "com.docker.compose.service")}}}'
    details = json.loads(command(["docker", "inspect", "--format", selected, container], timeout=30))
    image_fields = '{"registry_digests":{{json .RepoDigests}},"architecture":{{json .Architecture}},"os":{{json .Os}}}'
    image = json.loads(command(["docker", "image", "inspect", "--format", image_fields, details.pop("image_id")], timeout=30))
    image["registry_digests"] = image.get("registry_digests") or []
    return {**details, **image}


def capture_project_images(project):
    safe_id(project)
    containers = command(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project], timeout=30).decode().split()
    return [capture_image_metadata(container) for container in containers]


def artifact_record(path, stage):
    checked_path(str(path))
    if path.is_dir():
        return {"relative_path": path.relative_to(stage).as_posix(), "entry_type": "directory", "size": 0, "sha256": None}
    if not path.is_file():
        raise BackupError("unsupported staging artifact")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"relative_path": path.relative_to(stage).as_posix(), "entry_type": "file", "size": path.stat().st_size, "sha256": digest.hexdigest()}


def sqlite_baseline(path):
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            db.execute("PRAGMA trusted_schema=OFF")
            names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            counts = {}
            for name in names:
                quoted = '"' + name.replace('"', '""') + '"'
                counts[name] = db.execute("SELECT count(*) FROM " + quoted).fetchone()[0]
        return {"tables": counts}
    except sqlite3.Error as exc:
        raise BackupError("exported sqlite baseline failed") from exc


def build_manifest(stage: Path, config: dict, exports: dict) -> dict:
    stage = checked_path(str(stage))
    job_id = safe_id(config["job_id"])
    job = config["jobs"][job_id]
    deployment = {key: config.get("deployment", {}).get(key) for key in ("revision", "dirty", "vault_revision")}
    limitations = []
    if not deployment["revision"]:
        limitations.append("deployment_revision_unavailable")
    sources = {}
    for source in job["sources"]:
        identifier = safe_id(source["id"])
        exported = exports[identifier]
        record = {key: exported[key] for key in ("version", "database", "role", "extensions", "image", "engines", "optional_absent") if key in exported}
        record.update(kind=source["kind"], source_path=source.get("path"), artifacts=[])
        if not exported.get("optional_absent"):
            root = checked_path(str(stage / identifier))
            if not root.is_dir():
                raise BackupError("export artifact directory missing")
            record["artifacts"] = [artifact_record(path, stage) for path in [root, *sorted(root.rglob("*"))]]
        if source["kind"] == "sqlite" and not exported.get("optional_absent"):
            record["verification"] = {"baseline": sqlite_baseline(root / "database.sqlite"), "status": "captured_from_export"}
        elif source["kind"] in ("postgres", "mariadb"):
            record["verification"] = {"baseline": None, "status": "unavailable"}
            limitations.append("baseline_unavailable:" + identifier)
        else:
            record["verification"] = {"baseline": None, "status": "not_applicable"}
        sources[identifier] = record
    images = {}
    for service, project in job.get("service_projects", {}).items():
        safe_id(service)
        images[service] = capture_project_images(project)
        if not images[service] or any(not item.get("registry_digests") for item in images[service]):
            limitations.append("image_digest_unavailable:" + service)
    return {"schema_version": 2, "completed_exports_at": datetime.now(timezone.utc).isoformat(),
            "host": config["host"], "job": job_id, "deployment": deployment,
            "sources": sources, "images": images, "limitations": limitations,
            "excluded": ["photos", "videos", "downloads", "attachments", "raw_database_volumes"]}

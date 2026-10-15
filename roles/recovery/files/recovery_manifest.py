"""Source-scoped snapshots and explicit adapters for legacy/v2 metadata."""
from datetime import datetime
import json
from pathlib import PurePosixPath
import re

import yaml

from backup_common import BackupError, restic, safe_id
from recovery_models import KINDS, source_scope, validate_artifact


def select_snapshot(repo: dict, scope: dict) -> dict:
    scope = source_scope(scope)
    tag = "homelab-config-db:" + scope["job"]
    args = ["snapshots", "--json", "--host", scope["host"], "--tag", tag, "--path", scope["staging_path"]]
    snapshots = json.loads(restic(repo, args, capture=True))
    candidates = [item for item in snapshots if item.get("hostname") == scope["host"]
                  and tag in item.get("tags", []) and scope["staging_path"] in item.get("paths", [])
                  and not any(t.startswith("homelab-pending:") for t in item.get("tags", []))]
    if scope["snapshot"] == "latest":
        candidates.sort(key=lambda item: datetime.fromisoformat(item["time"].replace("Z", "+00:00")), reverse=True)
        candidates = candidates[:1]
    else:
        candidates = [item for item in candidates if item["id"].startswith(scope["snapshot"])]
    if len(candidates) != 1 or not re.fullmatch(r"[a-f0-9]{64}", candidates[0]["id"]):
        raise BackupError("snapshot is not an unambiguous completed source version")
    selected = candidates[0]
    nodes = [json.loads(line) for line in restic(repo, ["ls", "--json", selected["id"]], capture=True).splitlines() if line.strip()]
    manifests = [node["path"] for node in nodes if node.get("type") == "file" and PurePosixPath(node.get("path", "")).name == "manifest.json"]
    if len(manifests) != 1:
        raise BackupError("snapshot must contain one recovery manifest")
    manifest = PurePosixPath(manifests[0])
    if not manifest.is_absolute() or ".." in manifest.parts:
        raise BackupError("invalid snapshot tree path")
    return {"snapshot_id": selected["id"], "host": scope["host"], "job": scope["job"],
            "staging_path": scope["staging_path"], "snapshot_time": selected["time"],
            "tree_root": str(manifest.parent), "manifest_path": str(manifest)}


def read_compose_fields(content: str) -> dict:
    if len(content.encode()) > 1024 * 1024:
        raise BackupError("compose metadata too large")
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise BackupError("invalid compose metadata") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("services"), dict) or len(parsed["services"]) > 64:
        raise BackupError("invalid compose services")
    result = {}
    for name, service in parsed["services"].items():
        safe_id(name)
        if not isinstance(service, dict) or not isinstance(service.get("image"), str):
            raise BackupError("missing compose image")
        environment = service.get("environment", {})
        if isinstance(environment, list):
            if any(not isinstance(item, str) or "=" not in item for item in environment):
                raise BackupError("unsupported compose environment")
            environment = dict(item.split("=", 1) for item in environment)
        if not isinstance(environment, dict) or len(environment) > 256:
            raise BackupError("invalid compose environment")
        if any(not isinstance(key, str) or not isinstance(value, (str, int, bool, type(None))) for key, value in environment.items()):
            raise BackupError("invalid compose environment value")
        result[name] = {"image": service["image"], "environment": environment}
    return result


def normalize_manifest(raw: dict, selection: dict, legacy_profile: dict) -> dict:
    if not isinstance(raw, dict) or raw.get("schema_version", 1) not in (1, 2):
        raise BackupError("unsupported manifest schema")
    legacy = raw.get("schema_version", 1) == 1
    if not isinstance(raw.get("sources"), dict) or not raw["sources"]:
        raise BackupError("missing manifest sources")
    if not legacy and (raw.get("host") != selection["host"] or raw.get("job") != selection["job"]):
        raise BackupError("manifest source does not match selected snapshot")
    sources = {}
    for identifier, value in raw["sources"].items():
        safe_id(identifier)
        if not isinstance(value, dict):
            raise BackupError("invalid source metadata")
        value = dict(value)
        if legacy:
            definition = legacy_profile.get("sources", {}).get(identifier, {})
            kind = definition.get("kind", value.get("kind"))
            if kind not in KINDS:
                raise BackupError("legacy source requires an explicit kind")
            for key in ("version", "database", "role", "extensions", "preload_libraries", "definers", "verification"):
                if key in definition: value[key] = definition[key]
            suffix = {"postgres": "database.dump", "mariadb": "database.sql", "sqlite": "database.sqlite", "aria2_session": "aria2.session"}.get(kind)
            entry_type = "file" if suffix else definition.get("entry_type", "directory")
            relative = identifier + "/" + suffix if suffix else definition.get("artifact", identifier)
            value.update(kind=kind, source_path=definition.get("source_path"), artifacts=[] if value.get("optional_absent") else [
                {"relative_path": relative, "entry_type": entry_type, "size": definition.get("size", 0), "sha256": None}])
        if value.get("kind") not in KINDS or not isinstance(value.get("artifacts"), list):
            raise BackupError("invalid source kind or artifacts")
        artifacts = [validate_artifact(item, identifier, legacy) for item in value["artifacts"]]
        if not artifacts and not value.get("optional_absent"):
            raise BackupError("required source has no artifacts")
        if len({item["relative_path"] for item in artifacts}) != len(artifacts):
            raise BackupError("duplicate artifact path")
        value["artifacts"] = artifacts
        sources[identifier] = value
    images = legacy_profile.get("images", {}) if legacy else raw.get("images", {})
    if not isinstance(images, dict):
        raise BackupError("invalid image metadata")
    for service, values in images.items():
        safe_id(service)
        if not isinstance(values, list) or any(not isinstance(item, dict) or not isinstance(item.get("image"), str) for item in values):
            raise BackupError("invalid service image metadata")
    limitations = list(raw.get("limitations", []))
    if legacy:
        limitations.append("legacy_snapshot")
    return {"schema_version": raw.get("schema_version", 1), "selection": dict(selection),
            "services": sorted(images), "sources": sources, "images": images,
            "deployment": raw.get("deployment", {}), "limitations": limitations}


def validate_compatibility(model: dict, profile: dict) -> list[dict]:
    issues = []
    for service in profile.get("services", model.get("services", [])):
        images = model.get("images", {}).get(service, [])
        if not images:
            issues.append({"service": service, "code": "image_metadata_unavailable"})
        for image in images:
            digests = image.get("registry_digests") or []
            if not digests or any(not isinstance(item, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:-]*@sha256:[a-f0-9]{64}", item) for item in digests):
                issues.append({"service": service, "code": "image_digest_unavailable"})
            if image.get("architecture") != profile.get("architecture", "amd64"):
                issues.append({"service": service, "code": "image_architecture_mismatch"})
    return issues

"""Small validated JSON contracts; never use snapshot paths as target paths."""
from pathlib import PurePosixPath
import re

from backup_common import BackupError, safe_id

KINDS = {"postgres", "mariadb", "sqlite", "files", "aria2_session"}


def relative_artifact(value):
    if not isinstance(value, str) or not value or any(c in value for c in ("\\", "\0", "\n", "\r")):
        raise BackupError("invalid artifact path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value in (".", ".."):
        raise BackupError("invalid artifact path")
    return path.as_posix()


def source_scope(value):
    if not isinstance(value, dict):
        raise BackupError("invalid recovery source")
    for key in ("repository", "job"):
        safe_id(value.get(key))
    if not isinstance(value.get("host"), str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,252}", value["host"]):
        raise BackupError("invalid source host")
    staging = value.get("staging_path")
    if not isinstance(staging, str) or not staging.startswith("/") or ".." in PurePosixPath(staging).parts or any(c in staging for c in "\0\n\r\\"):
        raise BackupError("invalid source staging path")
    snapshot = value.get("snapshot")
    if snapshot != "latest" and (not isinstance(snapshot, str) or not re.fullmatch(r"[a-f0-9]{8,64}", snapshot)):
        raise BackupError("invalid snapshot selector")
    return {key: value[key] for key in ("repository", "host", "job", "staging_path", "snapshot")}


def validate_artifact(item, source_id, legacy=False):
    if not isinstance(item, dict) or item.get("entry_type") not in ("file", "directory"):
        raise BackupError("invalid artifact record")
    relative = relative_artifact(item.get("relative_path"))
    if PurePosixPath(relative).parts[0] != source_id:
        raise BackupError("artifact belongs to another source")
    size = item.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise BackupError("invalid artifact size")
    digest = item.get("sha256")
    if item["entry_type"] == "file" and not (legacy and digest is None):
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise BackupError("invalid artifact digest")
    return {"source_id": source_id, "relative_path": relative, "entry_type": item["entry_type"], "size": size, "sha256": digest}

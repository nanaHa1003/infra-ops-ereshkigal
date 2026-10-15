"""Secret-safe shared restic transport and filesystem validation."""
import os
from pathlib import Path
import re
import subprocess


class BackupError(Exception):
    """A sanitized operational failure; never include raw subprocess output."""


def safe_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", value):
        raise BackupError("invalid identifier")
    return value


def checked_path(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise BackupError("invalid absolute path")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise BackupError("symlink source or destination rejected")
    return path


class ResticError(BackupError):
    """Expose status and fixed error categories, never subprocess stderr."""
    def __init__(self, returncode, stderr):
        self.returncode = returncode
        message = "restic operation failed (exit " + str(returncode) + ")"
        lowered = stderr.lower()
        if b"access denied" in lowered or b"accessdenied" in lowered:
            message += ": S3 AccessDenied; check key bucket/prefix and capabilities"
        elif returncode == 12:
            message += ": repository password rejected"
        elif returncode == 10:
            message += ": repository not found"
        super().__init__(message)


def restic(repo, args, *, cwd=None, timeout=3600, capture=False):
    # Keep credentials out of argv; replace inherited provider credentials.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("AWS_", "RESTIC_", "B2_"))}
    env.update({"RESTIC_REPOSITORY": repo["url"], "RESTIC_PASSWORD": repo["restic_password"],
                "AWS_ACCESS_KEY_ID": repo["access_key_id"], "AWS_SECRET_ACCESS_KEY": repo["secret_access_key"]})
    if os.environ.get("RESTIC_CACHE_DIR"):
        env["RESTIC_CACHE_DIR"] = os.environ["RESTIC_CACHE_DIR"]
    try:
        result = subprocess.run(["restic", *args], env=env, cwd=cwd, stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError("restic unavailable or timed out") from exc
    if result.returncode:
        raise ResticError(result.returncode, result.stderr)
    return result.stdout or b""

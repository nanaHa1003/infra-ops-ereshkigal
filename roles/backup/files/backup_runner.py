#!/usr/bin/env python3
"""Root-only scheduled backup runner. Public logs never include raw errors."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import urllib.parse
import urllib.request

from backup_sources import BackupError, checked_path, prepare_sources, safe_id


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


def validate_config(config):
    state = checked_path(config["state_root"])
    if len(state.parts) < 3:
        raise BackupError("unsafe state root")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,252}", config["host"]):
        raise BackupError("invalid backup host")
    for name, repo in config["repositories"].items():
        safe_id(name)
        for key in ("url", "access_key_id", "secret_access_key", "restic_password"):
            if not isinstance(repo.get(key), str) or not repo[key].strip():
                raise BackupError("repository field missing")
        if any(char in repo["url"] for char in "\n\r\0"):
            raise BackupError("invalid repository URL")
    for name, job in config["jobs"].items():
        safe_id(name)
        if job["repository"] not in config["repositories"] or not job["sources"]:
            raise BackupError("invalid job repository or empty source list")
        seen = set()
        for source in job["sources"]:
            identifier = safe_id(source["id"])
            if identifier in seen:
                raise BackupError("duplicate source identifier")
            seen.add(identifier)
            if source["kind"] in ("postgres", "mariadb"):
                safe_id(source["container"])
            elif source["kind"] in ("files", "sqlite", "aria2_session"):
                checked_path(source["path"])
            else:
                raise BackupError("invalid source kind")
        ping = job.get("healthchecks_ping_url", "")
        if ping:
            parsed = urllib.parse.urlsplit(ping)
            if parsed.scheme != "https" or parsed.hostname != "hc-ping.com" or parsed.query or parsed.fragment:
                raise BackupError("invalid Healthchecks URL")
    return state


@contextmanager
def repository_lock(config):
    state = validate_config(config)
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.chmod(0o700)
    lockpath = checked_path(str(state / "repository.lock"))
    with lockpath.open("a") as lock:
        lockpath.chmod(0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("another backup operation is running") from exc
        yield state


def notify(job, event):
    url = job.get("healthchecks_ping_url", "")
    if not url:
        return True
    suffix = {"start": "/start", "success": "", "fail": "/fail"}[event]
    try:
        with urllib.request.urlopen(urllib.request.Request(url.rstrip("/") + suffix,
                                    data=b"", method="POST"), timeout=15) as response:
            return response.status == 200
    except Exception:
        print("Healthchecks delivery failed", file=sys.stderr)
        return False


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


def job_scope(config, job_id, pending=False):
    tag = ("homelab-pending:" if pending else "homelab-config-db:") + job_id
    return ["--host", config["host"], "--tag", tag,
            "--path", str(Path(config["state_root"]) / "staging" / job_id)]


def run_job(config, job_id):
    os.umask(0o077)
    job = None
    stage = None
    locked = False
    try:
        safe_id(job_id)
        with repository_lock(config) as state:
            locked = True
            job = config["jobs"][job_id]
            stage = checked_path(str(state / "staging" / job_id))
            try:
                if stage.exists():
                    shutil.rmtree(stage)
                delivered = notify(job, "start")
                metadata = prepare_sources(job["sources"], stage)
                (stage / "manifest.json").write_text(json.dumps({"completed_exports_at": datetime.now(timezone.utc).isoformat(),
                                                                "sources": metadata}, indent=2))
                repo = config["repositories"][job["repository"]]
                result = restic(repo, ["backup", "--json", "--host", config["host"],
                                      "--tag", "homelab-pending:" + job_id, "."], cwd=stage, capture=True)
                summaries = [json.loads(line) for line in result.splitlines()]
                snapshot = next(item["snapshot_id"] for item in summaries if item.get("message_type") == "summary")
                if not re.fullmatch(r"[a-f0-9]{8,64}", snapshot):
                    raise BackupError("invalid backup summary")
                restic(repo, ["tag", "--add", "homelab-config-db:" + job_id,
                              "--remove", "homelab-pending:" + job_id, snapshot])
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
            delivered = notify(job, "success") and delivered
            print("Backup uploaded" if delivered else "Backup uploaded; monitoring delivery failed")
            return 0 if delivered else 2
    except Exception:
        print("Backup failed; inspect source availability, credentials and service status", file=sys.stderr)
        if locked and job:
            notify(job, "fail")
        return 1


def maintenance(config, job_id, preview=False):
    os.umask(0o077)
    try:
        safe_id(job_id)
        with repository_lock(config):
            job = config["jobs"][job_id]
            retention = job["retention"]
            args = ["forget", *job_scope(config, job_id), "--group-by", "host,paths,tags",
                    "--keep-within", retention["within"], "--keep-daily", str(retention["daily"]),
                    "--keep-weekly", str(retention["weekly"]), "--keep-monthly", str(retention["monthly"]),
                    "--dry-run" if preview else "--prune"]
            repo = config["repositories"][job["repository"]]
            result = restic(repo, ["--json", *args] if preview else args, capture=preview)
            if preview:
                groups = json.loads(result) if result else []
                print(json.dumps({"keep": [snapshot["id"] for group in groups for snapshot in group.get("keep", [])],
                                  "remove": [snapshot["id"] for group in groups for snapshot in group.get("remove", [])]}))
            if not preview:
                restic(repo, ["forget", *job_scope(config, job_id, pending=True), "--group-by", "host,paths,tags",
                              "--keep-within", "48h", "--keep-last", "1", "--prune"])
                restic(repo, ["check"])
        print("Retention preview completed" if preview else "Maintenance completed")
        return 0
    except Exception:
        print("Maintenance failed", file=sys.stderr)
        return 1


def check_data(config, job_id, full=False):
    os.umask(0o077)
    try:
        with repository_lock(config) as state:
            job = config["jobs"][safe_id(job_id)]
            counter = state / (job_id + "-check-quarter")
            quarter = int(counter.read_text()) if counter.exists() else 1
            if quarter not in range(1, 5):
                raise BackupError("invalid check rotation")
            restic(config["repositories"][job["repository"]],
                   ["check", "--read-data"] if full else ["check", "--read-data-subset", f"{quarter}/4"])
            if not full:
                counter.write_text(str(quarter % 4 + 1))
        print("Data check completed")
        return 0
    except Exception:
        print("Data check failed", file=sys.stderr)
        return 1


def scoped_snapshots(config, job_id):
    """Caller must hold the repository lock."""
    job = config["jobs"][safe_id(job_id)]
    result = restic(config["repositories"][job["repository"]],
                    ["snapshots", "--json", *job_scope(config, job_id)], capture=True)
    return [{key: snapshot[key] for key in ("id", "time", "hostname", "tags", "paths")}
            for snapshot in json.loads(result)]


def snapshots(config, job_id):
    with repository_lock(config):
        return scoped_snapshots(config, job_id)


def ensure_initialized(config, repository_id):
    """Initialize only on restic's unambiguous repository-missing exit code."""
    with repository_lock(config):
        repo = config["repositories"][safe_id(repository_id)]
        try:
            restic(repo, ["snapshots", "--json"], capture=True)
            return False
        except ResticError as error:
            if error.returncode != 10:
                raise
        restic(repo, ["init"])
        return True


def restore_job(config, job_id, snapshot):
    """Restore only into a new protected scratch directory, never in place."""
    os.umask(0o077)
    if snapshot != "latest" and not re.fullmatch(r"[a-f0-9]{8,64}", snapshot):
        raise BackupError("invalid snapshot identifier")
    with repository_lock(config) as state:
        job = config["jobs"][safe_id(job_id)]
        candidates = scoped_snapshots(config, job_id)
        if snapshot == "latest":
            candidates = sorted(candidates, key=lambda item: datetime.fromisoformat(item["time"].replace("Z", "+00:00")), reverse=True)[:1]
        else:
            candidates = [item for item in candidates if item["id"].startswith(snapshot)]
        if len(candidates) != 1:
            raise BackupError("snapshot is not an unambiguous completed version of this job")
        snapshot = candidates[0]["id"]
        target = state / "restores" / (job_id + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"))
        checked_path(str(target))
        target.mkdir(mode=0o700, parents=True)
        restic(config["repositories"][job["repository"]], ["restore", snapshot, *job_scope(config, job_id),
                                                          "--target", str(target), "--verify"])
        return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/homelab-backup/config.json")
    parser.add_argument("operation", choices=["validate", "backup", "init", "ensure-init", "maintenance", "retention-preview", "check", "snapshots", "restore"])
    parser.add_argument("identifier")
    parser.add_argument("--full", action="store_true", help="Read all repository data during check")
    parser.add_argument("--snapshot", default="latest", help="Snapshot ID for scratch restore")
    args = parser.parse_args()
    os.umask(0o077)
    def interrupted(signum, frame):
        raise BackupError("operation interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGALRM, interrupted)
    signal.alarm(6600)
    try:
        config_path = checked_path(args.config)
        if config_path.stat().st_mode & 0o077:
            raise BackupError("configuration permissions must be root-only")
        config = json.loads(config_path.read_text())
        if args.operation == "validate":
            validate_config(config)
            for job in config["jobs"].values():
                for source in job["sources"]:
                    if "path" in source and not source.get("optional") and not Path(source["path"]).exists():
                        raise BackupError("required source absent")
            print("Backup configuration and required paths validated")
            return 0
        if args.operation == "backup":
            return run_job(config, args.identifier)
        if args.operation in ("maintenance", "retention-preview"):
            return maintenance(config, args.identifier, args.operation == "retention-preview")
        if args.operation == "check":
            return check_data(config, args.identifier, args.full)
        if args.operation == "snapshots":
            print(json.dumps(snapshots(config, args.identifier), indent=2))
            return 0
        if args.operation == "ensure-init":
            changed = ensure_initialized(config, args.identifier)
            print("Repository initialized" if changed else "Repository already initialized")
            return 0
        if args.operation == "restore":
            print("Restored to protected scratch directory: " + str(restore_job(config, args.identifier, args.snapshot)))
            return 0
        with repository_lock(config):
            restic(config["repositories"][safe_id(args.identifier)], ["init"])
        print("Repository initialized")
        return 0
    except Exception as error:
        detail = str(error) if isinstance(error, BackupError) else "unexpected internal error"
        print("Operation failed: " + detail, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

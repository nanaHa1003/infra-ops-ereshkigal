"""Prepare consistent database exports and explicitly allowlisted files."""
from contextlib import closing
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import time
import urllib.request


from backup_common import BackupError, checked_path, safe_id


def command(args, *, output=None, input_file=None, timeout=1200):
    """Only fixed scripts may use sh; no user value is inserted into shell code."""
    try:
        result = subprocess.run(args, stdin=input_file, stdout=output or subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError("source command unavailable or timed out") from exc
    if result.returncode:
        raise BackupError("source command failed")
    return result.stdout


def stable_copy(source, destination):
    """Reject symlinks and retry if a writer changed the file during copying."""
    for _ in range(3):
        checked_path(str(source))
        before = source.stat()
        if not stat.S_ISREG(before.st_mode):
            raise BackupError("non-regular source rejected")
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as reader, destination.open("wb") as writer:
            opened = os.fstat(reader.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise BackupError("source changed while opening")
            shutil.copyfileobj(reader, writer)
        destination.chmod(0o600)
        after = source.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
                after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            return
    raise BackupError("source changed during export")


def copy_files(source, destination, excludes):
    if not source.is_dir():
        stable_copy(source, destination / source.name)
        return
    def scan_error(error):
        raise BackupError("required directory could not be scanned") from error
    for root, dirs, files in os.walk(source, followlinks=False, onerror=scan_error):
        root = Path(root)
        relative = root.relative_to(source)
        def excluded(name):
            return any(fnmatch.fnmatch(name, pattern) or
                       fnmatch.fnmatch((relative / name).as_posix(), pattern) for pattern in excludes)
        dirs[:] = [name for name in dirs if not excluded(name)]
        for name in dirs + files:
            if not excluded(name) and (root / name).is_symlink():
                raise BackupError("symlink source rejected")
        target = destination / relative
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in files:
            if not excluded(name):
                stable_copy(root / name, target / name)


def sqlite_export(source, destination):
    deadline = time.monotonic() + 1200
    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise BackupError("sqlite backup timed out")
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)) as original:
            with closing(sqlite3.connect(destination)) as backup:
                original.backup(backup, pages=256, progress=progress)
                backup.execute("PRAGMA journal_mode=DELETE")
                if backup.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise BackupError("sqlite integrity check failed")
    except sqlite3.Error as exc:
        raise BackupError("sqlite export failed") from exc
    destination.chmod(0o600)


PG_PREFIX = 'export PGPASSWORD="$POSTGRES_PASSWORD"; '
PG_DUMP = PG_PREFIX + 'exec pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom --no-owner --no-acl'
PG_METADATA = PG_PREFIX + '''exec psql --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --no-psqlrc --tuples-only --no-align --command="SELECT json_build_object('version',version(),'database',current_database(),'role',current_user,'extensions',(SELECT json_agg(json_build_object('name',extname,'version',extversion)) FROM pg_extension))"'''
MYSQL_PREFIX = 'export MYSQL_PWD="$MYSQL_PASSWORD"; '
MYSQL_ENGINES = MYSQL_PREFIX + '''exec mariadb --user="$MYSQL_USER" --database="$MYSQL_DATABASE" --batch --skip-column-names --execute="SELECT DISTINCT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE() AND TABLE_TYPE='BASE TABLE'"'''
MYSQL_DUMP = MYSQL_PREFIX + 'exec mariadb-dump --user="$MYSQL_USER" --single-transaction --quick --routines --events --triggers --hex-blob --skip-lock-tables --databases "$MYSQL_DATABASE"'


def database_export(source, destination):
    container = safe_id(source["container"])
    prefix = ["docker", "exec", container]
    image = command(["docker", "inspect", "--format", "{{.Config.Image}}", container], timeout=30).decode().strip()
    if source["kind"] == "postgres":
        metadata = json.loads(command(prefix + ["sh", "-c", PG_METADATA]))
        target = destination / "database.dump"
        with target.open("wb") as output:
            command(prefix + ["sh", "-c", PG_DUMP], output=output)
        with target.open("rb") as archive:
            command(["docker", "exec", "-i", container, "pg_restore", "--list"], input_file=archive)
    else:
        engines = command(prefix + ["sh", "-c", MYSQL_ENGINES]).decode().split()
        if any(engine.upper() != "INNODB" for engine in engines):
            raise BackupError("nontransactional MariaDB tables require a separate consistency plan")
        metadata = {"version": command(prefix + ["mariadb", "--version"]).decode().strip(), "engines": engines}
        target = destination / "database.sql"
        with target.open("wb") as output:
            command(prefix + ["sh", "-c", MYSQL_DUMP], output=output)
    if not target.stat().st_size:
        raise BackupError("empty database export")
    target.chmod(0o600)
    metadata["image"] = image
    return metadata


def prepare_sources(sources, destination):
    destination = checked_path(str(destination))
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    seen = set()
    metadata = {}
    for source in sources:
        identifier = safe_id(source["id"])
        if identifier in seen:
            raise BackupError("duplicate source identifier")
        seen.add(identifier)
        print("Exporting source: " + identifier, flush=True)
        kind = source["kind"]
        target = destination / identifier
        target.mkdir(mode=0o700)
        if kind in ("postgres", "mariadb"):
            metadata[identifier] = database_export(source, target)
            continue
        path = checked_path(source["path"])
        if not path.exists():
            if source.get("optional", False):
                metadata[identifier] = {"optional_absent": True}
                continue
            raise BackupError("required source missing")
        if destination == path or path in destination.parents:
            raise BackupError("source contains staging directory")
        if kind == "sqlite":
            sqlite_export(path, target / "database.sqlite")
        elif kind == "files":
            copy_files(path, target, source.get("exclude", []))
        elif kind == "aria2_session":
            payload = json.dumps({"jsonrpc": "2.0", "id": "backup", "method": "aria2.saveSession",
                                  "params": ["token:" + source["rpc_secret"]]}).encode()
            request = urllib.request.Request(source["rpc_url"], data=payload,
                                             headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=30) as response:
                if json.load(response).get("result") != "OK":
                    raise BackupError("aria2 session export failed")
            stable_copy(path, target / "aria2.session")
        else:
            raise BackupError("unknown source kind")
        metadata[identifier] = {"kind": kind}
    return metadata

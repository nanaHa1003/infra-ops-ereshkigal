"""Run-owned, network-isolated database imports with separate application roles."""
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import sqlite3
import subprocess
import time
from contextlib import closing

from backup_common import BackupError, checked_path, safe_id
from recovery_files import build_mappings, _artifact_path
from recovery_state import PHASES, advance_service, save_state

DB_IMAGES = {"auth": "auth_postgres_image", "n8n": "n8n_postgres_image", "immich": "immich_postgres_image", "wordpress": "wordpress_mariadb_image"}
PRELOADS = {"vchord.so", "vectors.so", "pg_stat_statements", "auto_explain"}


def _docker(args, *, input_bytes=None, input_file=None, env=None, timeout=1200):
    try:
        result = subprocess.run(["docker", *args], input=input_bytes, stdin=input_file,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, **(env or {})}, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError("database Docker operation unavailable or timed out") from exc
    if result.returncode:
        raise BackupError("database Docker or SQL operation failed")
    return result.stdout


def identifier(value):
    if not isinstance(value, str) or not value or "\0" in value or len(value.encode()) > 63:
        raise BackupError("invalid database identifier")
    return '"' + value.replace('"', '""') + '"'


def literal(value):
    if not isinstance(value, str) or "\0" in value:
        raise BackupError("invalid database credential value")
    return "'" + value.replace("'", "''") + "'"


def _credentials(service, target):
    values = target["secrets"]
    if service == "auth": return "authentik", "authentik", values["authentik"]["pgpass"]
    if service == "n8n":
        row = values["n8n"]; return row["postgres_user"], row["postgres_db"], row["postgres_password"]
    if service == "immich":
        row = values["immich"]; return row["db_username"], row["db_database_name"], row["db_password"]
    if service == "wordpress":
        row = values["wordpress"]; return row["mysql_user"], row["mysql_database"], row["mysql_password"]
    raise BackupError("database service not supported")


def database_spec(service, model, target, state):
    rows = [row for row in build_mappings(model, target, target["registry"]) if row["service"] == service and row["kind"] in ("postgres", "mariadb", "sqlite")]
    if not rows: return None
    if len(rows) != 1: raise BackupError("service database source is ambiguous")
    row = rows[0]
    source = model["sources"][row["source_id"]]
    spec = {"service": service, "kind": row["kind"], "source_id": row["source_id"], "source": source,
        "artifact": str(_artifact_path(row, Path(target["scratch_root"]))), "data_root": row["service_root"],
        "target_file": row["target"], "name": "recovery-db-" + state["run_id"] + "-" + service,
        "network": "recovery-" + state["run_id"], "run_id": state["run_id"]}
    safe_id(spec["name"])
    if spec["kind"] == "sqlite": return spec
    user, database, password = _credentials(service, target)
    identifier(user); identifier(database); literal(password)
    if not password or user == "recovery_admin" or database in ("template0", "template1"):
        raise BackupError("unsupported application database identity")
    if source.get("role", user) != user or source.get("database", database) != database:
        raise BackupError("source database identity does not match recovery Vault")
    image = target["image_variables"][DB_IMAGES[service]]
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:-]*@sha256:[a-f0-9]{64}", image):
        raise BackupError("pinned database image required")
    spec.update(app_user=user, database=database, app_password=password, admin_user="recovery_admin", image=image)
    version = source.get("version", "")
    if spec["kind"] == "postgres":
        match = re.search(r"PostgreSQL\s+([0-9]+(?:\.[0-9]+)*)", version)
        if not match or int(match[1].split(".")[0]) not in (14, 16, 18):
            raise BackupError("unsupported or missing PostgreSQL source version")
        spec.update(version=match[1], major=int(match[1].split(".")[0]))
        preload = source.get("preload_libraries")
        if preload is None:
            names = {item["name"] for item in source.get("extensions", [])}
            preload = ",".join(name + ".so" for name in ("vchord", "vectors") if name in names)
        if not isinstance(preload, str): raise BackupError("invalid PostgreSQL preload settings")
        libraries = [value.strip() for value in preload.split(",") if value.strip()]
        if any(value not in PRELOADS for value in libraries):
            raise BackupError("unsupported PostgreSQL preload library")
        spec.update(preload_libraries=libraries, import_role="recovery_import_" + state["run_id"].replace("-", "_") + "_" + service)
        identifier(spec["import_role"])
        spec["mount"] = "/var/lib/postgresql" if spec["major"] >= 18 else "/var/lib/postgresql/data"
    else:
        match = re.search(r"Distrib\s+([0-9]+\.[0-9]+\.[0-9]+)", version) or re.match(r"([0-9]+\.[0-9]+\.[0-9]+)", version)
        if not match: raise BackupError("missing MariaDB source version")
        if user == "root": raise BackupError("WordPress application account must not be root")
        spec.update(version=match[1], mount="/var/lib/mysql", admin_user="root")
        spec["allowed_definers"] = target.get("database_options", {}).get(service, {}).get("allowed_definers", [user + "@%"])
    return spec


def running_volume_users(path):
    users = []
    for container in _docker(["ps", "-q"]).decode().split():
        mounts = json.loads(_docker(["inspect", "--format", "{{json .Mounts}}", container]))
        if any(item.get("Source") == str(path) for item in mounts): users.append(container)
    return users


def _ensure_network(spec, state):
    names = _docker(["network", "ls", "--filter", "label=homelab.recovery.run=" + state["run_id"], "--format", "{{.Name}}"]).decode().split()
    if spec["network"] not in names:
        if spec["network"] not in state.setdefault("owned_networks", []): state["owned_networks"].append(spec["network"])
        save_state(state)
        _docker(["network", "create", "--internal", "--label", "homelab.recovery.run=" + state["run_id"], spec["network"]], timeout=60)
    network = json.loads(_docker(["network", "inspect", "--format", '{"internal":{{json .Internal}},"run":{{json (index .Labels "homelab.recovery.run")}}}', spec["network"]]))
    if not network.get("internal") or network.get("run") != state["run_id"]:
        raise BackupError("recovery database network is not isolated and owned")


def create_database_container(spec, state):
    root = checked_path(spec["data_root"])
    if str(root) not in state["_approved_roots"]:
        raise BackupError("database root is not approved for this run")
    if root.exists() and any(root.iterdir()): raise BackupError("database data directory is not empty")
    if str(root) not in state["owned_paths"]: state["owned_paths"].append(str(root))
    if spec["name"] not in state["owned_containers"]: state["owned_containers"].append(spec["name"])
    save_state(state)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if spec["kind"] == "postgres" and spec["major"] >= 18:
        uid = _docker(["run", "--rm", "--network", "none", "--entrypoint", "id", spec["image"], "-u", "postgres"], timeout=120).decode().strip()
        if not uid.isdigit(): raise BackupError("PostgreSQL image user could not be resolved")
        os.chown(root, int(uid), -1)
    args = ["run", "-d", "--name", spec["name"], "--label", "homelab.recovery.run=" + state["run_id"],
        "--network", spec["network"], "--network-alias", spec["name"], "--security-opt", "no-new-privileges",
        "--volume", str(root) + ":" + spec["mount"] + ":Z"]
    env = {}
    if spec["kind"] == "postgres":
        env["POSTGRES_PASSWORD"] = secrets.token_urlsafe(32)
        args += ["--env", "POSTGRES_PASSWORD", "--env", "POSTGRES_USER=recovery_admin", "--env", "POSTGRES_DB=postgres",
                 "--env", "POSTGRES_INITDB_ARGS=--auth-local=trust --data-checksums", spec["image"], "postgres"]
        if spec["service"] == "immich": args += ["-c", "config_file=/etc/postgresql/postgresql.conf"]
        if spec["preload_libraries"]: args += ["-c", "shared_preload_libraries=" + ",".join(spec["preload_libraries"])]
    else:
        env["MARIADB_ROOT_PASSWORD"] = secrets.token_urlsafe(32)
        args += ["--env", "MARIADB_ROOT_PASSWORD", spec["image"], "--event-scheduler=OFF"]
    runtime = {key: spec[key] for key in ("name", "network", "data_root", "kind", "service", "image", "database", "app_user", "admin_user")}
    runtime["mount"] = spec["mount"]
    state.setdefault("databases", {})[spec["service"]] = runtime
    save_state(state)
    _docker(args, env=env, timeout=180)


def pg_sql(spec, sql, database="postgres"):
    script = 'export PGPASSWORD="$POSTGRES_PASSWORD"; exec psql --username="$POSTGRES_USER" --no-psqlrc --tuples-only --no-align --set=ON_ERROR_STOP=1 "$@"'
    return _docker(["exec", "-i", spec["name"], "sh", "-c", script, "sh", "--dbname", database], input_bytes=sql.encode()).decode().strip()


def mysql_sql(spec, sql, database=None, tcp=False):
    script = 'export MYSQL_PWD="$MARIADB_ROOT_PASSWORD"; exec mariadb --user=root --batch --skip-column-names "$@"'
    args = ["exec", "-i", spec["name"], "sh", "-c", script, "sh"]
    if tcp: args += ["--host=127.0.0.1"]
    if database: args += ["--database", database]
    return _docker(args, input_bytes=sql.encode()).decode().strip()


def wait_database(spec):
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            if spec["kind"] == "postgres":
                _docker(["exec", spec["name"], "pg_isready", "-h", "127.0.0.1", "-U", "recovery_admin", "-d", "postgres"], timeout=10)
            else: mysql_sql(spec, "SELECT 1;", tcp=True)
            return
        except BackupError: time.sleep(1)
    raise BackupError("database readiness timed out")


def postgres_setup_sql(spec):
    user, database, temporary = identifier(spec["app_user"]), identifier(spec["database"]), identifier(spec["import_role"])
    sql = "SET standard_conforming_strings = on;\n"
    sql += "CREATE ROLE " + user + " LOGIN NOSUPERUSER PASSWORD " + literal(spec["app_password"]) + ";\n"
    sql += ("ALTER DATABASE " + database + " OWNER TO " if spec["database"] == "postgres" else "CREATE DATABASE " + database + " OWNER ") + user + ";\n"
    sql += "CREATE ROLE " + temporary + " NOLOGIN SUPERUSER;\n"
    return sql


def _restore_postgres(spec, state):
    _ensure_network(spec, state)
    create_database_container(spec, state)
    wait_database(spec)
    release = pg_sql(spec, "SHOW server_version;").split()[0]
    if release != spec["version"]: raise BackupError("PostgreSQL source and target release mismatch")
    data_directory = PurePosixPath(pg_sql(spec, "SHOW data_directory;"))
    mount = PurePosixPath(spec["mount"])
    if data_directory != mount and mount not in data_directory.parents:
        raise BackupError("PostgreSQL data is outside the persistent mount")
    cluster = Path(spec["data_root"]).joinpath(*data_directory.relative_to(mount).parts)
    if not (cluster / "PG_VERSION").is_file(): raise BackupError("PostgreSQL persistent cluster missing")
    pg_sql(spec, postgres_setup_sql(spec))
    with checked_path(spec["artifact"]).open("rb") as archive:
        script = 'export PGPASSWORD="$POSTGRES_PASSWORD"; exec pg_restore --username="$POSTGRES_USER" --exit-on-error --no-owner --no-acl "$@"'
        _docker(["exec", "-i", spec["name"], "sh", "-c", script, "sh", "--dbname", spec["database"], "--role", spec["import_role"]], input_file=archive)
    pg_sql(spec, "REASSIGN OWNED BY " + identifier(spec["import_role"]) + " TO " + identifier(spec["app_user"]) + ";", spec["database"])
    pg_sql(spec, "DROP ROLE " + identifier(spec["import_role"]) + ";")
    if spec["preload_libraries"]:
        pg_sql(spec, "ALTER SYSTEM SET shared_preload_libraries = " + literal(",".join(spec["preload_libraries"])) + ";")
    installed = json.loads(pg_sql(spec, "SELECT json_object_agg(extname,extversion) FROM pg_extension;", spec["database"]))
    for extension in spec["source"].get("extensions", []):
        if installed.get(extension["name"]) != extension["version"]:
            raise BackupError("PostgreSQL extension version mismatch")
    superuser = pg_sql(spec, "SELECT rolsuper FROM pg_roles WHERE rolname=" + literal(spec["app_user"]) + ";")
    if superuser != "f": raise BackupError("application role must not be a bootstrap superuser")
    return {"engine_version": release, "extensions": installed, "app_role_nonsuperuser": True}


def _mysql_identifier(value):
    identifier(value)
    return "`" + value.replace("`", "``") + "`"


def _restore_mariadb(spec, state):
    _ensure_network(spec, state)
    create_database_container(spec, state)
    wait_database(spec)
    version = mysql_sql(spec, "SELECT VERSION();").split("-", 1)[0]
    if version != spec["version"]: raise BackupError("MariaDB source and target release mismatch")
    account = literal(spec["app_user"]) + "@'%'"
    database = _mysql_identifier(spec["database"])
    setup = "SET SESSION sql_mode='NO_BACKSLASH_ESCAPES';\nCREATE DATABASE " + database + ";\nCREATE USER " + account + " IDENTIFIED BY " + literal(spec["app_password"]) + ";\nGRANT ALL PRIVILEGES ON " + database + ".* TO " + account + ";\n"
    for definer in spec["source"].get("definers", []):
        if definer not in spec["allowed_definers"]: raise BackupError("MariaDB DEFINER is not approved")
        user, host = definer.rsplit("@", 1)
        if definer != spec["app_user"] + "@%" and definer != "root@localhost":
            setup += "CREATE USER IF NOT EXISTS " + literal(user) + "@" + literal(host) + " ACCOUNT LOCK;\n"
            setup += "GRANT ALL PRIVILEGES ON " + database + ".* TO " + literal(user) + "@" + literal(host) + ";\n"
    mysql_sql(spec, setup)
    script = 'export MYSQL_PWD="$MARIADB_ROOT_PASSWORD"; exec mariadb --user=root "$@"'
    with checked_path(spec["artifact"]).open("rb") as archive:
        _docker(["exec", "-i", spec["name"], "sh", "-c", script, "sh"], input_file=archive)
    query = "SELECT DISTINCT DEFINER FROM (SELECT DEFINER FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA=" + literal(spec["database"]) + " UNION SELECT DEFINER FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=" + literal(spec["database"]) + " UNION SELECT DEFINER FROM information_schema.EVENTS WHERE EVENT_SCHEMA=" + literal(spec["database"]) + " UNION SELECT DEFINER FROM information_schema.VIEWS WHERE TABLE_SCHEMA=" + literal(spec["database"]) + ") AS object_definers;"
    definers = mysql_sql(spec, query).splitlines()
    if any(value not in spec["allowed_definers"] for value in definers): raise BackupError("restored MariaDB DEFINER is not approved")
    grants = mysql_sql(spec, "SHOW GRANTS FOR " + account + ";")
    if "ON *.*" in grants and any(word in grants for word in ("ALL PRIVILEGES ON *.*", "SUPER")):
        raise BackupError("application MariaDB account has global privileges")
    return {"engine_version": version, "definers": definers, "app_grants_scoped": True}


def _restore_sqlite(spec, state):
    database = checked_path(spec["target_file"])
    if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-shm")):
        raise BackupError("stale SQLite WAL or SHM must be resolved before recovery")
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise BackupError("restored SQLite integrity check failed")
    except sqlite3.Error as exc: raise BackupError("restored SQLite could not be read") from exc
    return {"sqlite_integrity": True}


def restore_database(service: str, model: dict, target: dict, state: dict) -> dict:
    current = state["services"][service]
    if PHASES.index(current["phase"]) >= PHASES.index("imported"):
        return state["results"][service]
    if current["phase"] != "prepared" or current.get("database_import") in ("in_progress", "failed"):
        raise BackupError("database import requires prepared state and explicit cleanup after failure")
    spec = database_spec(service, model, target, state)
    if spec is None:
        result = {"service": service, "status": "not_applicable", "checks": {}, "counts": {}, "limitations": []}
        advance_service(state, service, "imported", result)
        return result
    artifact = checked_path(spec["artifact"])
    if not artifact.is_file() or not artifact.stat().st_size:
        raise BackupError("empty or missing recovery database dump")
    if spec["kind"] == "postgres":
        with artifact.open("rb") as stream:
            if stream.read(5) != b"PGDMP": raise BackupError("invalid PostgreSQL archive")
    if spec["kind"] != "sqlite" and running_volume_users(spec["data_root"]):
        raise BackupError("database volume is already used by a running container")
    current["database_import"] = "in_progress"
    save_state(state)
    try:
        checks = {"postgres": _restore_postgres, "mariadb": _restore_mariadb, "sqlite": _restore_sqlite}[spec["kind"]](spec, state)
        result = {"service": service, "status": "passed", "checks": checks, "counts": {}, "limitations": []}
        current["database_import"] = "complete"
        advance_service(state, service, "imported", result)
        return result
    except Exception as error:
        current["database_import"] = "failed"
        state["results"][service] = {"service": service, "status": "failed", "checks": {}, "counts": {}, "limitations": ["database_import_failed"]}
        save_state(state)
        if isinstance(error, BackupError): raise
        raise BackupError("database import failed") from error


def stop_recovery_databases(state: dict) -> None:
    for runtime in state.get("databases", {}).values():
        owner = json.loads(_docker(["inspect", "--format", '{{json (index .Config.Labels "homelab.recovery.run")}}', runtime["name"]]))
        if owner != state["run_id"]: raise BackupError("database container ownership mismatch")
        _docker(["stop", "--time", "30", runtime["name"]], timeout=60)

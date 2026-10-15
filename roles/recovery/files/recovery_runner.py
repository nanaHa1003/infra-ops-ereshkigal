#!/usr/bin/env python3
"""Explicit recovery phases. Sensitive inputs are protected config or stdin."""
import argparse
import hashlib
import json
import os
import platform
from pathlib import Path, PurePosixPath
import secrets
import signal
import shutil
import sys

# Source checkout and installed program directory share the same entry point.
shared = Path(__file__).resolve().parents[2] / "backup/files"
if shared.is_dir(): sys.path.insert(0, str(shared))
from backup_common import BackupError, checked_path, restic, safe_id
from backup_sources import command
from recovery_manifest import normalize_manifest, select_snapshot, validate_compatibility
from recovery_models import relative_artifact, source_scope
from recovery_files import build_mappings, compare_recovery_secrets, install_artifacts, validate_artifacts, validate_targets
from recovery_state import PHASES, advance_service, cleanup_preview, open_run, run_lock, save_state

from recovery_databases import restore_database, stop_recovery_databases, _docker, running_volume_users, wait_database
from recovery_activate import begin_activation, finish_activation, check_activation_ready, validate_activation_environment
from recovery_verify import verify_service, write_report

HOST_LOCK_ROOT = '/run/homelab-recovery'


def runtime_lock():
    # Host-wide location is independent of a user-selected state directory.
    return run_lock(HOST_LOCK_ROOT)


IMAGE_VARIABLES = {
    "proxy": {"caddy": "proxy_caddy_image", "docker-ro": "proxy_socket_proxy_image", "docker-rw": "proxy_socket_proxy_image"},
    "auth": {"postgresql": "auth_postgres_image", "redis": "auth_redis_image", "authentik-server": "auth_authentik_image", "authentik-worker": "auth_authentik_image"},
    "n8n": {"n8n": "n8n_image", "n8n-postgres": "n8n_postgres_image"},
    "immich": {"database": "immich_postgres_image", "redis": "immich_redis_image", "immich-server": "immich_server_image", "immich-machine-learning": "immich_ml_image"},
    "wordpress": {"wordpress": "wordpress_image", "mariadb": "wordpress_mariadb_image"},
    "mealie": {"mealie": "mealie_image"}, "pinchflat": {"pinchflat": "pinchflat_image"},
    "homepage": {"homepage": "homepage_image"}, "aria2": {"aria2": "aria2_image"},
}


def preflight_issues(config):
    target = config["target"]
    probe = target.get("probe", {})
    issues = []
    if (target.get("distribution"), str(target.get("release")), target.get("architecture")) != ("Fedora", "43", "amd64"):
        issues.append({"code": "unsupported_target_platform"})
    if not probe.get("mount_ok"):
        issues.append({"code": "storage_mount_missing"})
    if probe.get("free_bytes", 0) < target.get("min_free_bytes", 0) or target.get("min_free_bytes", 0) <= 0:
        issues.append({"code": "insufficient_free_space"})
    for field, code in (("package_conflicts", "conflicting_container_packages"), ("nonempty_roots", "nonempty_service_destinations"),
                        ("conflicting_services", "existing_production_containers"), ("active_backup_timers", "backup_timers_already_active"),
                        ("ddns_active", "ddns_already_active")):
        if probe.get(field): issues.append({"code": code})
    if probe.get("service_probe_failed"):
        issues.append({"code": "target_service_state_unknown"})
    if not target.get("machine_id"):
        issues.append({"code": "target_identity_unavailable"})
    if not shutil.which("restic"):
        issues.append({"code": "source_tool_unavailable"})
    return issues


def _absolute(value):
    if not isinstance(value, str) or not value.startswith("/") or any(c in value for c in "\0\n\r\\") or ".." in PurePosixPath(value).parts:
        raise BackupError("invalid target path")
    return PurePosixPath(value)


def profile_payload(config):
    target = config["target"]
    return {"source": {key: config["source"][key] for key in ("repository", "host", "job", "staging_path")},
            "repository_url": config["repository"]["url"], "services": config["services"],
            "state_root": config["state_root"], "registry": config.get("registry", {}),
            "permissions": target.get("permissions", []), "database_options": target.get("database_options", {}),
            "target": {key: target[key] for key in ("host", "machine_id", "storage_root", "stack_root", "storage_mount", "min_free_bytes", "distribution", "release", "architecture")},
            "legacy_profile": config.get("legacy_profile", {}), "vault_revision": config.get("vault_revision"), "domain": config.get("domain")}


def profile_hash(config):
    return hashlib.sha256(json.dumps(profile_payload(config), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _registry_roots(config):
    storage = _absolute(config["target"]["storage_root"])
    roots = set()
    stack = _absolute(config["target"]["stack_root"])
    destinations = []
    for permission in config["target"].get("permissions", []):
        roots.add(str(storage / relative_artifact(permission["path"])))
    for service in config["services"]:
        roots.add(str(stack / service))
        if service == "proxy": roots.add(str(stack / "docker-proxy"))
        safe_id(service)
        if service not in config["registry"]:
            raise BackupError("unsupported selected service")
        for identifier, definition in config["registry"][service]["sources"].items():
            if definition["layout"] == "reference": continue
            relative = relative_artifact(definition["target"])
            root = definition.get("root", relative if definition["layout"] in ("directory", "database") else str(PurePosixPath(relative).parent))
            roots.add(str(storage / relative_artifact(root)))
            destinations.append({"source_id": identifier, "target": str(storage / relative)})
    return sorted(roots), destinations


def _write_private(path, value):
    path = checked_path(str(path))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump(value, output, indent=2)
        output.flush()
        os.fsync(output.fileno())


def create_plan(config):
    source_scope(config["source"])
    if not config["services"] or len(set(config["services"])) != len(config["services"]):
        raise BackupError("explicit unique recovery service list required")
    for name in ("storage_root", "stack_root"):
        if len(_absolute(config["target"][name]).parts) < 3:
            raise BackupError("target root is too broad")
    state_root = _absolute(config["state_root"])
    storage, stack = _absolute(config["target"]["storage_root"]), _absolute(config["target"]["stack_root"])
    if len(state_root.parts) < 4 or any(a == b or a in b.parents or b in a.parents for a, b in ((state_root, storage), (state_root, stack), (storage, stack))):
        raise BackupError("recovery roots overlap or state root is too broad")
    roots, destinations = _registry_roots(config)
    issues = preflight_issues(config)
    selection, model, image_variables = None, None, {}
    if not issues:
        selection = select_snapshot(config["repository"], config["source"])
        raw = json.loads(restic(config["repository"], ["dump", selection["snapshot_id"], selection["manifest_path"]], capture=True))
        model = normalize_manifest(raw, selection, config.get("legacy_profile", {}))
        issues += validate_compatibility(model, {"services": config["services"], "architecture": config["target"]["architecture"]})
        for service in config["services"]:
            required = IMAGE_VARIABLES[service]
            for image in model["images"].get(service, []):
                variable = required.get(image.get("service"))
                if variable and image.get("registry_digests"):
                    pinned = sorted(image["registry_digests"])[0]
                    if variable in image_variables and image_variables[variable] != pinned:
                        issues.append({"code": "conflicting_service_images", "service": service})
                    image_variables[variable] = pinned
            if any(variable not in image_variables for variable in set(required.values())):
                issues.append({"code": "service_image_mapping_incomplete", "service": service})
    run_id = secrets.token_hex(12)
    plan = {"run_id": run_id, "selection": selection, "model": model, "services": config["services"],
            "target": config["target"], "profile_sha256": profile_hash(config), "approved_roots": roots,
            "destinations": destinations, "image_variables": image_variables, "blockers": issues}
    path = Path(config.get("plan_file") or Path.cwd() / ".recovery" / (run_id + ".plan.json"))
    _write_private(path, plan)
    return {"run_id": run_id, "plan_file": str(path), "snapshot_id": selection["snapshot_id"] if selection else None, "blockers": issues}


def load_plan(config, run_id=None):
    path = checked_path(config["plan_file"])
    if path.stat().st_mode & 0o077:
        raise BackupError("recovery plan permissions are not protected")
    plan = json.loads(path.read_text())
    if plan.get("blockers") or not plan.get("selection"):
        raise BackupError("recovery plan has unresolved blockers")
    if profile_hash(config) != plan["profile_sha256"]:
        raise BackupError("recovery target or profile no longer matches the plan")
    selector = source_scope(config["source"])["snapshot"]
    if selector != "latest" and not plan["selection"]["snapshot_id"].startswith(selector):
        raise BackupError("requested snapshot no longer matches the pinned plan")
    expected_roots, _ = _registry_roots(config)
    if expected_roots != plan.get("approved_roots") or plan.get("services") != config["services"]:
        raise BackupError("recovery destination plan changed")
    safe_id(plan["run_id"])
    if run_id and plan["run_id"] != run_id:
        raise BackupError("recovery run does not match selected plan")
    return plan


def download(config, plan):
    state = open_run(plan, Path(config["state_root"]))
    if state["activation"]["services_started"]:
        raise BackupError("restore refused after application activation")
    scratch = Path(state["_run_root"]) / "scratch"
    target = {**config["target"], "services": config["services"], "run_id": state["run_id"], "scratch_root": str(scratch),
              "wordpress_core_image": plan["image_variables"].get("wordpress_image")}
    mappings = build_mappings(plan["model"], target, config["registry"])
    validate_targets(mappings, state)
    for value in plan["approved_roots"]:
        root = checked_path(value)
        if root.exists() and (not root.is_dir() or any(root.iterdir())) and value not in state["owned_paths"]:
            raise BackupError("planned destination contains unowned data")
    if state["phase"] != "planned": return state
    source = {**config["source"], "snapshot": plan["selection"]["snapshot_id"]}
    selection = select_snapshot(config["repository"], source)
    if selection != plan["selection"]:
        raise BackupError("pinned source metadata changed")
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    restic(config["repository"], ["restore", selection["snapshot_id"], "--target", str(scratch), "--verify"])
    model = {**plan["model"], "services": config["services"]}
    validate_artifacts(mappings, scratch)
    checks = compare_recovery_secrets(model, scratch, config["secrets"])
    state["results"]["secret_checks"] = checks
    save_state(state)
    if any(not item["matched"] for item in checks):
        raise BackupError("historical Vault mismatch; inspect field-only recovery report")
    # Reserve only preflight-checked roots before Ansible prepares them.
    state["owned_paths"] = list(plan["approved_roots"])
    save_state(state)
    for value in plan["approved_roots"]:
        root = checked_path(value)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    for service in config["services"]:
        advance_service(state, service, "downloaded", {"secret_checks": [row for row in checks if row["service"] == service]})
    return state


def restore_and_verify(config, plan, state, verify_only):
    if state['activation']['services_started']: raise BackupError('data verification refused after activation')
    if verify_only and PHASES.index(state['phase']) < PHASES.index('imported'):
        raise BackupError('verification requires completed imports')
    scratch = Path(state['_run_root']) / 'scratch'
    target = {**config['target'], 'registry':config['registry'], 'secrets':config['secrets'], 'services':config['services'],
        'run_id':state.get('run_id'), 'scratch_root':str(scratch), 'image_variables':plan['image_variables'], 'wordpress_core_image':plan['image_variables'].get('wordpress_image')}
    model = {**plan['model'], 'services':config['services']}
    results=[]
    try:
        if state['phase']=='downloaded':
            install_artifacts(build_mappings(model,target,config['registry']),scratch,state)
            for service in config['services']: advance_service(state,service,'prepared',{})
        if state['phase'] not in ('prepared','imported','verified'): raise BackupError('restore phase not ready')
        if not verify_only:
            for service in config['services']: restore_database(service,model,target,state)
        for runtime in state.get('databases',{}).values():
            owner=json.loads(_docker(['inspect','--format','{{json (index .Config.Labels "homelab.recovery.run")}}',runtime['name']]))
            if owner!=state['run_id']: raise BackupError('database ownership mismatch')
            users=running_volume_users(runtime['data_root'])
            if users and any(value not in _docker(['ps','-q','--filter','name=^'+runtime['name']+'$']).decode().split() for value in users):
                raise BackupError('database volume already in use')
            _docker(['start',runtime['name']]); wait_database(runtime)
        for service in config['services']:
            try:
                result=verify_service(service,model,target,state)
            except Exception:
                failure={'service':service,'status':'failed','checks':{},'limitations':['verification_failed'],'activation_blockers':['verification_failed']}
                state['results'][service]=failure
                save_state(state)
                write_report(state,results+[failure],Path(state['_run_root'])/'report.json')
                raise BackupError('service verification failed; inspect protected report')
            advance_service(state,service,'verified',result)
            results.append(result)
        write_report(state,results,Path(state['_run_root'])/'report.json')
        return {'run_id':state['run_id'],'phase':state['phase'],'report':str(Path(state['_run_root'])/'report.json')}
    finally:
        stop_recovery_databases(state)


def cleanup_run(state):
    preview=cleanup_preview(state)
    existing_containers=set(_docker(['ps','-a','--format','{{.Names}}']).decode().split())
    existing_networks=set(_docker(['network','ls','--format','{{.Name}}']).decode().split())
    present=[]
    for item in preview:
        kind,value=item['kind'],item['value']
        if kind=='path': present.append(item);continue
        if value not in (existing_containers if kind=='container' else existing_networks): continue
        inspect=['inspect'] if kind=='container' else ['network','inspect']
        labels='.Config.Labels' if kind=='container' else '.Labels'
        owner=json.loads(_docker(inspect+['--format','{{json (index '+labels+' "homelab.recovery.run")}}',value]))
        if owner!=state['run_id']:raise BackupError('cleanup resource ownership mismatch')
        present.append(item)
    # Every present resource was validated before the first deletion.
    for item in present:
        kind,value=item['kind'],item['value']
        if kind=='container':_docker(['rm','-f','-v',value],timeout=60)
        elif kind=='network':_docker(['network','rm',value],timeout=60)
        elif checked_path(value).exists():shutil.rmtree(checked_path(value))
    shutil.rmtree(checked_path(state['_run_root']))


def install_signal_handlers():
    def interrupted(_number,_frame):
        raise BackupError('recovery interrupted')
    for number in (signal.SIGTERM,signal.SIGHUP,signal.SIGINT): signal.signal(number,interrupted)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--config")
    inputs.add_argument("--config-stdin", action="store_true")
    parser.add_argument("operation", choices=["plan", "validate-plan", "download", "restore", "verify", "cleanup-preview", "cleanup", "activation-begin", "activation-ready", "activation-finish"])
    parser.add_argument("--run")
    parser.add_argument("--service")
    args = parser.parse_args(argv)
    os.umask(0o077)
    install_signal_handlers()
    try:
        if args.config_stdin:
            config = json.load(sys.stdin)
        else:
            path = checked_path(args.config)
            if path.stat().st_mode & 0o077: raise BackupError("configuration permissions must be protected")
            config = json.loads(path.read_text())
        if args.operation == "plan":
            result = create_plan(config)
            print(json.dumps(result))
            return 1 if result["blockers"] else 0
        plan = load_plan(config, args.run)
        if args.operation == "validate-plan":
            blockers = [item for item in preflight_issues(config) if item["code"] not in ("nonempty_service_destinations", "existing_production_containers")]
            print(json.dumps({"run_id": plan["run_id"], "blockers": blockers}))
            return 1 if blockers else 0
        # Recovery always operates on this Linux target's daemon, never an inherited remote context.
        os.environ["DOCKER_HOST"] = "unix:///var/run/docker.sock"
        for key in ("DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
            os.environ.pop(key, None)
        actual_id = Path("/etc/machine-id").read_text().strip()
        operating_system = platform.freedesktop_os_release()
        if actual_id != plan["target"]["machine_id"] or operating_system.get("ID") != "fedora" or operating_system.get("VERSION_ID") != "43" or platform.machine() != "x86_64":
            raise BackupError("runtime target identity or platform does not match the recovery plan")
        with runtime_lock():
            if args.operation == "download":
                state = download(config, plan)
                print(json.dumps({"run_id": state["run_id"], "phase": state["phase"]}))
                return 0
            if args.operation in ("restore", "verify"):
                state = open_run(plan, Path(config["state_root"]))
                print(json.dumps(restore_and_verify(config, plan, state, args.operation == "verify")))
                return 0
            state = open_run(plan, Path(config["state_root"]))
            if args.operation.startswith('activation-'):
                options=config['activation']; selected=options['services']
                validate_activation_environment(config,plan,state,selected)
                if args.operation=='activation-begin':
                    result=begin_activation(state,selected,options.get('old_host_fenced') is True,options.get('fencing_reference'))
                elif args.operation=='activation-ready':
                    if args.service not in state['activation']['services_started']: raise BackupError('service was not authorized for activation')
                    result=check_activation_ready([args.service],config['target'])
                    state['activation'].setdefault('readiness',{}).update(result)
                    save_state(state)
                else:
                    result=finish_activation(state,selected,config['target'],options.get('cutover_complete') is True,options.get('enable_ddns') is True)
                print(json.dumps(result))
                return 0
            preview = cleanup_preview(state)
            if args.operation == "cleanup-preview":
                print(json.dumps({"run_id": state["run_id"], "resources": preview}))
                return 0
            if args.run != state["run_id"]:
                raise BackupError("cleanup requires an explicit matching run ID")
            cleanup_run(state)
            print(json.dumps({"run_id": args.run, "cleaned": True}))
            return 0
    except Exception as error:
        detail = str(error) if isinstance(error, BackupError) else "unexpected recovery error"
        print(json.dumps({"error": detail}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

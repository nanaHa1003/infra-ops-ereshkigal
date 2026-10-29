# Replacement-host recovery

`recover.yml` restores a pinned Restic snapshot onto a Linux host without
requiring SSH access to the source host. It is separate from `site.yml`.
B2 `database` and RustFS `rustfs_db` contain databases and configuration;
RustFS `rustfs_files` also contains the explicitly selected service media.
Aria2 downloads are excluded.

Recovery uses the metadata actually present in the selected snapshot. Current
manifests record image digests, database versions/extensions/preload libraries,
MariaDB DEFINER identities and the source domain. Older snapshots require an
explicit legacy profile for missing metadata.

## Prerequisites

- A repository checkout containing the roles/templates corresponding to the
  snapshot, Ansible collections, Python/YAML and Restic on the controller.
- The historical Ansible Vault, its unlock method, Restic password and scoped
  storage credentials. Repository access must allow creating/removing Restic
  locks. Keep these independently of the source and backup hosts. Do not place
  secrets in command arguments, reports or Git.
- A target with sudo, Docker Engine running Linux containers, Compose v2,
  Restic, Python 3/PyYAML, a stable machine ID, mounted storage and sufficient
  free space. Host provisioning is separate: recovery does not install or
  replace container engines or remove Podman/Buildah.
- On SELinux hosts, `semanage`, `restorecon` and Ansible SELinux Python bindings.
- Image digests with Linux OS and CPU architecture matching the target.
  Cross-architecture image substitution is not implemented. Resolve missing
  image/version metadata before proceeding; do not substitute `latest`.
- Local inventory/profile files copied from
  `inventory/recovery.ini.example` and
  `inventory/recovery-profile.yml.example`. Their working filenames are ignored
  by Git. Keep the target in `recovery_targets`, outside the production `homelab`
  group, until cutover is complete.

`recover_source.host`, `job` and `staging_path` describe the original backup.
`storage_root`, `stack_root` and `recover_storage_mount` describe the target and
can differ from source paths. Preserve the source `domain`; domain migration is
a separate operation. `recover_vault_revision` identifies the historical Vault.

For older manifests, `recover_legacy_profile` supplies source kinds, original
paths and image digests with service labels. Database metadata can include
`version`, `database`, `role`, `extensions`, `preload_libraries`, `definers` and
`verification`; the domain belongs in `recover_legacy_profile.domain`. Leave
unknown historical counts unavailable instead of using current live counts.

## Plan, restore and verify

Every invocation requires a single-host `--limit`. These inventory, profile and
plan paths are controller files. Planning does not change target services or
data. `--check` runs only the plan phase.

```sh
ansible-playbook recover.yml -i inventory/recovery.ini --limit replacement \
  -e @inventory/recovery-profile.yml -e recover_phase=plan --ask-become-pass
```

Retain `.recovery/<run_id>.plan.json`. It pins the full snapshot ID, source
scope, target machine ID, services, paths, image digests and profile hash.
Set `recover_plan_file` in the profile to its absolute path for subsequent
phases. Do not edit the generated plan. Changes to the target, service selection
or Vault revision require a new plan. Remove `recover_plan_file` or select an
unused filename before generating that new plan.

```sh
ansible-playbook recover.yml -i inventory/recovery.ini --limit replacement \
  -e @inventory/recovery-profile.yml -e recover_phase=restore --ask-become-pass
```

Restore installs recovery scripts without installing system packages, downloads
the pinned snapshot, validates artifacts and historical secrets, prepares
pinned Compose definitions, restores files and imports databases, then verifies
the result. Application servers, workflows, SMTP, DDNS, Healthchecks and backup
timers remain inactive. Temporary database containers have no published ports
or Docker socket and use an internal network. They stop on completion or
failure, preserving data. SQLite restoration rejects leftover WAL/SHM files.

The protected target report is
`/var/lib/homelab-recovery/runs/<run_id>/report.json`. Decrypted n8n credentials
exist only in the CLI container's tmpfs; reports contain counts, not secrets or
secret hashes. Missing historical row-count baselines produce limited
verification. Unsupported versions/schemas, decryption failures and missing
media can block activation.

Restore retries, verification and activation recheck actual historical
Compose/Vault fields; reusing a revision label does not bypass comparison.
Verification can be repeated without importing the databases again:

```sh
ansible-playbook recover.yml -i inventory/recovery.ini --limit replacement \
  -e @inventory/recovery-profile.yml -e recover_phase=verify --ask-become-pass
```

An interrupted database import is marked failed and is not automatically
reimported over existing data. Preview cleanup before removing a failed run:

```sh
ansible-playbook recover.yml -i inventory/recovery.ini --limit replacement \
  -e @inventory/recovery-profile.yml -e recover_phase=cleanup-preview \
  -e recover_run_id=RUN_ID --ask-become-pass
ansible-playbook recover.yml -i inventory/recovery.ini --limit replacement \
  -e @inventory/recovery-profile.yml -e recover_phase=cleanup \
  -e recover_run_id=RUN_ID --ask-become-pass
```

Cleanup is limited to recorded run-owned directories, containers and networks.
The host-wide `/var/lib/homelab-recovery/ownership.json` reserves paths across
runs, including empty directories; do not delete it manually. Cleanup refuses
paths mounted by foreign running containers and is prohibited once application
startup begins.

## Activation and cutover

1. If the source host is available, stop application writes and create a final
   successful backup. Plan, restore and verify that exact snapshot.
2. Fence the source host's services, schedules, external routing and DDNS so it
   cannot resume writes or receive traffic. Retain a fencing reference.
3. Review the protected report. For limited verification without historical
   baselines, record service-specific review in `recover_accept_limited`.
   This cannot override failures, unknown schemas or missing-media blockers.
4. Select `recover_activate_services` from restored services and include their
   dependencies. Proxy/Homepage require Authentik; Homepage activates with Proxy.
   Services requiring missing media remain stopped. Restore media to the expected
   nonempty paths and provide `recover_media_evidence.<service>.reference`.
   Path checks do not establish content completeness or database/media consistency.
5. Set `recover_old_host_fenced: true`, `recover_fencing_reference`,
   `recover_cutover_complete: false` and `recover_enable_ddns: false`, then run:

```sh
ansible-playbook recover.yml -i inventory/recovery.ini --limit replacement \
  -e @inventory/recovery-profile.yml -e recover_phase=activate --ask-become-pass
```

Activation records startup intent, stops recovery databases, starts the pinned
services through their normal role entry points and checks database/application
readiness. Failures stop subsequent steps. Socket proxies start only after
fencing; Caddy starts last. The resulting `awaiting_cutover` state requires
manual DNS/routing changes. Applications may already perform background work
and accept writes.

Switch DNS/routing, verify login, SSO, data and external connectivity, then set
`recover_cutover_complete: true` and rerun activation with all services whose
startup has begun. DDNS is installed only with explicit
`recover_enable_ddns: true`.

There is no automatic rollback after application startup or target writes.
To return to the source host, stop the target, preserve its new data, reconcile
changes and switch routing manually. Recovery does not restart the source or
restore DNS automatically.

After cutover, create production inventory, scoped backup storage credentials
and a separate Healthchecks check. Verify the new job/source host before
running `backup.yml`. Recovery does not inherit source timers or ping URLs or
modify the controller's production inventory. Run `site.yml` separately after
cutover and version checks.

## Host portability

Recovery checks capabilities instead of using a distribution/version allowlist.
It still verifies Linux, machine ID, CPU architecture, POSIX permissions and
Docker bind-mount behavior. WSL2 should use Linux storage; with Docker Desktop,
verify that the daemon can access the same target paths. CLI connectivity alone
is insufficient. Systemd is optional when absent; failed timer inspection on an
active systemd host blocks recovery. Backup scheduling and DDNS require their
own host integration.

## Snapshot formats and media

For RustFS full-service recovery, select repository/job `rustfs_files` and the
matching source host and `/var/lib/homelab-backup/staging/rustfs_files` scope.
Store credentials under the matching repository ID in Vault.

Manifest v1/v2 database/configuration snapshots remain supported. v3 also
records direct media roots at their original snapshot paths. Recovery maps data
into approved target roots; original paths cannot select target destinations.
The manifest is selected within the scoped staging path, avoiding confusion
with an application's own `manifest.json`.

Staged database/configuration artifacts have explicit SHA-256 hashes. Restic
verifies direct content during scratch restoration; installation requires a
successful `restore --verify` for the exact pinned snapshot. Full WordPress HTML
replaces narrower WordPress file mappings. Approved directory ancestors can
coexist with separately exported SQLite databases. File collisions, traversal
and symlink escapes are rejected. Aria2 session data is optional in historical
backups and absent from newly produced jobs.

Validate media references and application behavior before activation. Successful
file restoration alone does not establish a complete, coherent application
library. Recovery time depends on snapshot size, storage/network throughput and
database import/index work; measure it with the intended target and dataset.

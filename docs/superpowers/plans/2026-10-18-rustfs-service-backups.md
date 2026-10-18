# RustFS Service Backups Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline. The user approved the specification and explicitly requested implementation on an independent branch.

**Goal:** Independent B2 and RustFS database/full-service backups with no local Healthchecks and verified recovery.
**Architecture:** Per-job protected configs select a single repository. URL-scoped locks, separate caches/staging, direct media roots and v3 manifests preserve v1/v2 recovery.
**Tech Stack:** Python, Ansible, systemd, Restic S3, existing unittest/Molecule/container CI.
**Spec:** `docs/superpowers/specs/2026-10-18-rustfs-service-backup-design.md`.

## Global Constraints

- Preserve existing `database` job, repository URL/password, snapshot tag and staging path.
- Local jobs are `rustfs_db` and `rustfs_files`; Healthchecks explicitly disabled.
- Do not copy media into staging. Do not back up raw live database files.
- Aria2 downloads/session, LANraragi and unrelated services excluded.
- No live deployment, production initialization/pruning or application upgrades during implementation.
- Preserve user's uncommitted encrypted Vault in the main checkout.

## Review Focus

- Another malformed repository must not block the selected job; test configuration scoping.
- URL aliases must share a lock while distinct destinations can execute concurrently; test actual lock handles/processes.
- Direct-source exclusions must match what Restic archives, including changing/symlink files; test real snapshot listings.
- A direct tree may contain `manifest.json`; select only the scoped manifest, and reject manifest path manipulation.
- Restores with nested SQLite and full directory sources must reject file collisions while allowing approved directory ancestors.

## Task 1: Isolated execution and optional monitoring

Files: `roles/backup/files/backup_runner.py`, new `backup_runtime.py`, `backup_common.py`, `tests/test_backup_isolation.py`.
Interfaces: `scoped_config(config, job_id) -> dict`, `repository_lock(config, repository_id=None) -> contextmanager`, `load_config(path, operation, identifier) -> dict`; runner public functions keep signatures.
- [ ] Add tests for failed unrelated credentials/sources, URL aliases and independent locks, disabled monitoring with a stale URL, per-job cache and success state.
- [ ] Run failing tests, implement selected-config validation, URL-based locks, scoped caches and no-op notification.
- [ ] Preserve legacy CLI and timing defaults; add bounded operation timeouts and process-group termination in the Restic transport.
- [ ] Run backup unit tests and commit.

## Task 2: Direct media capture and v3 manifests

Files: new `roles/backup/files/backup_direct.py`, `backup_sources.py`, `backup_manifest.py`, `backup_runner.py`, `tests/test_backup_direct.py`.
Interfaces: `scan_direct(source) -> dict`, `prepare_direct(sources) -> dict`, `verify_direct(sources, inventories) -> None`; direct files use existing `kind=files` with `capture=direct`.
- [ ] Add failing tests for no staging copy, traversal/symlink rejection, exclusions, missing/optional roots, live SQLite exclusion and mutation detection.
- [ ] Archive staged exports plus absolute direct roots in one Restic snapshot; derive v3 manifests only when direct capture is present. Keep v2 hashes unchanged.
- [ ] Promote pending snapshot only after unchanged-source verification; return failure on partial read.
- [ ] Run direct/manifest/runner tests and commit.

## Task 3: Ansible jobs and service inventory

Files: backup tasks/templates/defaults, host vars, CI Vault stub, Molecule backup fixtures, `tests/test_backup_ansible.py`.
- [ ] Add failing render tests for local configs without monitoring secrets, per-job services/config paths, explicit init selection and exact source coverage/exclusions.
- [ ] Deploy protected per-job configs and per-job timeout overrides. Preserve aggregate config for legacy CLI; selected runtime must isolate other jobs.
- [ ] Add three independent jobs/schedules/retention. Reuse source lists without recursive inventory-template expansion.
- [ ] Prevent migration while old service processes are active, without stopping them; initialize only selected repositories when opted in.
- [ ] Run Ansible render/contract tests, lint and syntax checks; commit.

## Task 4: Backward-compatible direct artifact recovery

Files: recovery manifest/models/files/verify/registry, `tests/test_recovery_direct.py`.
- [ ] Add failing v3 normalization/path/hash-policy/mapping tests plus legacy fixture checks.
- [ ] Accept v3 direct roots with explicit source-ID-relative paths; no target path comes from a snapshot root. Null hashes allowed only for validated direct v3 records, and only after verified Restic restore.
- [ ] Resolve full WordPress source replacements and n8n key preservation; permit approved directory ancestors of excluded live SQLite paths without permitting file collisions.
- [ ] Add media source mappings; Aria2 session optional. Keep media activation evidence conservative.
- [ ] Run recovery suite and commit.

## Task 5: Real round trips, failure/concurrency acceptance and documentation

Files: `tests/test_backup_integration.py`, new `tests/test_backup_independent_integration.py`, `docs/backup.md`, `docs/recovery.md`.
- [ ] Use disposable real Restic repositories to back up/restore direct media with a staged SQLite export; compare hashes and test a media file named manifest.json.
- [ ] Hold one repository lock in another process and back up another repository successfully. Inject bad destination and monitoring failures, then verify unaffected snapshots.
- [ ] Run full suite; run available container/syntax/lint checks and disclose skips precisely.
- [ ] Document safe separate deployment/init/start/status/restore commands and local no-monitoring behavior.
- [ ] Obtain one independent whole-branch review; fix important findings with regression tests. Commit changes, leave branch ready for review; do not merge or deploy.

## Execution Ledger

Pre-flight: shared interfaces defined above; legacy single-repository lock callers remain supported. User instruction authorizes inline implementation; no additional plan approval requested.

Task 1: runtime isolation RED→GREEN; backup suite 41 tests passed (1 integration skip). Ruling: keep `repository_lock(config)` for existing single-repository callers and add an explicit repository selector for multi-repository administrative calls; lock identity uses normalized URL, not alias.
Task 2: direct-source and manifest RED→GREEN; 46 backup tests passed (1 real-Restic test unavailable locally). Direct roots are directories, with staged files remaining the existing safe-copy path. No media copies or synthetic hashes are created.
Task 3: production render tests RED→GREEN; 50 backup tests passed (1 integration skip), backup role lint and backup.yml syntax check passed. Ruling: use `extra_sources` and `exclude_source_ids` when rendering the full job, preserving YAML anchors for the common allowlist; retain staged n8n key/Pinchflat extras and explicitly exclude those paths from their direct parents to avoid duplicate file recovery mappings.

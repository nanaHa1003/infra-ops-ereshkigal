# Independent RustFS backups for managed homelab services

## Objective and approved constraints

Extend the existing Ansible backup role to protect the nine enabled services on
Ereshkigal through Hecate's RustFS, accessed through NetBird. Keep the existing B2
database/configuration backup operational and independently recoverable. Do not
upgrade applications as part of this work.

The user has explicitly requested that a failed destination/job must not stop
other backup jobs, and that local RustFS jobs omit Healthchecks entirely. Aria2
downloads and session state are excluded. LANraragi, Nextcloud, TubeArchivist,
Home Assistant, retired New-API and unrelated host data are out of scope.

This is an architectural extension of the existing backup and recovery flows:
large-file capture, snapshot metadata and recovery mapping change together.
Implementation follows review of this specification and its implementation plan.

## Existing evidence

- Source data filesystem: ext4, approximately 693 GiB used and 978 GiB available.
  The 621 GiB LANraragi directory is excluded from this project.
- Immich media: approximately 39 GiB, including 32 GiB original library data;
  its separate Docker `/data` volume was empty at inventory time.
- Pinchflat: approximately 9 GiB, principally downloads.
- Source system filesystem has approximately 101 GiB free. Do not duplicate media
  into system-disk staging as an intermediate step.
- RustFS endpoint `http://hecate.netbird.cloud:9000` is reachable from Ereshkigal.
- Both planned buckets passed authenticated S3 put/get/range/delete,
  multipart complete/abort, and source-host Restic 0.19.1 init/backup/full check/
  restore/hash comparison/forget/prune. Unique test prefixes were cleaned.
  Production repositories have not been initialized by this validation.
- Vault contains `repositories.database`, `repositories.rustfs_db` and
  `repositories.rustfs_files`. RustFS credentials are currently shared between
  its two repositories; repository encryption passwords are independent.

## Selected approach and alternatives

Use independent source-to-destination backup jobs with Restic's S3 backend.
Each local job exports its own databases rather than depending on a B2 upload,
B2 download, another job's staging directory or another job's snapshot.

Do not use a B2-to-RustFS replication chain: it introduces the failure dependency
that the user explicitly ruled out. Do not mirror raw repository objects with a
bidirectional sync: it obscures retention ownership and snapshot acceptance.

All jobs still share the source host, Docker, disks and network. Destination
failure isolation does not provide isolation from source host outages, full
filesystems or competing I/O. Apply low I/O priority and test concurrency; retain
B2 as the off-host/provider-independent copy of databases and configuration.

## Jobs, repositories and schedules

All schedules use Asia/Taipei. Preserve existing B2 host/job identifiers, scope,
repository URL and encryption password so old snapshots remain discoverable.

| Job / repository | Destination | Backup | Maintenance | Data check | Healthchecks |
| --- | --- | --- | --- | --- | --- |
| `database` / `database` | Existing B2 repository | 00/06/12/18:00 | Existing Sunday 03:00 | Existing monthly rotating quarter | Existing enabled configuration |
| `rustfs_db` / `rustfs_db` | `homelab-db-config/ereshkigal` | 00/06/12/18:20 | Sunday 03:30 | Sunday 09:00, rotating quarter | Explicitly disabled |
| `rustfs_files` / `rustfs_files` | `homelab-services/ereshkigal` | Daily 02:00 | Sunday 04:30 | Sunday 10:00, rotating quarter | Explicitly disabled |

RustFS URLs use `s3:http://hecate.netbird.cloud:9000/<bucket>/ereshkigal`.
Access is restricted through NetBird and RustFS IAM. Do not substitute the 9001
Console endpoint. Keep bucket Object Lock/versioning disabled for this initial
operating model and do not add object-expiration lifecycle rules.

Database/config retention stays `within=48h, daily=14, weekly=8, monthly=12`.
Local full-service retention uses `within=48h, daily=14, weekly=8, monthly=6`.
The existing rotating-quarter check algorithm can be scheduled weekly for the
local repositories; it is not a complete application restore test.

## Scope and source representation

The common allowlist retains existing database exports, keys, configuration,
custom prompts/templates, Compose files, Caddy state and DDNS script. Remove
`aria2-session` from all newly produced jobs and stop calling Aria2 RPC. Retain
legacy session adapters in recovery so historical backups remain readable.

`rustfs_db` uses the same common sources as B2 and exports them independently.
`rustfs_files` includes its own consistent database exports and common settings,
plus direct file sources below. Use explicit source definitions; never scan all
of `/mnt/storage`, `/`, Docker volumes or the RustFS data directory.

| Service | Additional local file protection |
| --- | --- |
| Authentik | Entire `authentik/media`; existing templates and Compose retained |
| WordPress | Entire `wordpress/html`, including uploads, core, themes, plugins and configuration |
| Immich | Entire `immich/library`, including originals, thumbnails, transcoded video, profile and built-in backup files |
| Mealie | `mealie/data` excluding exact live SQLite DB/WAL/SHM paths; retain online SQLite export and prompts |
| Homepage | Existing complete persisted configuration |
| n8n | `n8n/data`, including encryption config and filesystem binary data; exclude known logs/cache and unused live SQLite files |
| Pinchflat | Complete config excluding live SQLite database/WAL/SHM paths, plus downloads; retain online SQLite export |
| Aria2 | Existing allowlisted config/settings/filter/scripts and Compose; no downloads, session or DHT caches |
| Proxy / DDNS | Existing protected Caddy state/config, proxy definitions and DDNS script |

Within the full-service job, replace narrower overlapping WordPress/n8n/Pinchflat
file sources with their complete directory sources rather than creating duplicate
recovery destinations. A service source can be replaced only when its required
configuration/key coverage is preserved and tested by the recovery registry.
Live DB files must never be smuggled into a file backup through a parent directory.
Only genuinely optional directories (for example an absent WordPress uploads
folder) can be marked optional; missing required media roots fail that job.

Exact exclusion paths must be verified against each deployed service's directory
layout before encoding the allowlist; no archive/media filenames enter public logs.

## Execution isolation and failure handling

1. Render a protected config per job containing only that job, its repository and
   necessary secrets. systemd units select the per-job config, not an aggregate
   config which could reject B2 because another repository is malformed.
2. Give each job separate staging, check rotation state and cache directories.
   Staging contains only DB exports and selected small settings, not media copies.
3. Lock by physical repository URL, not a single global lock and not merely a
   configurable alias. Different destination URLs can run concurrently; operations
   on the same URL cannot concurrently prune/backup/check/init/restore. Busy work
   produces a sanitized result limited to that repository. Do not create a job
   chain, `Requires=` dependencies, or a shared all-jobs execution service.
4. Preserve legacy CLI/config compatibility for existing commands and deployed
   units during migration. Unit/config switching must not interrupt an already
   running backup. Deployment should not reset B2's timer or success state.
5. Support explicitly selected repository initialization through Ansible. Default
   initialization remains off; wrong password/access errors never trigger init.
   Initializing RustFS must not require successfully connecting to B2.
6. Make Healthchecks an explicit per-job option, defaulting to the existing B2
   behavior. `false` must avoid both Vault job URL lookup and all outbound pings.
   Do not create RustFS Healthchecks entries. Local results remain visible through
   journalctl, systemd exit status and per-job last-success metadata.
7. Retain sanitized logs and environment-only provider credentials. A failed or
   partial Restic upload remains pending and is not selected as a completed backup.
8. Give backup/maintenance/check operations job-specific, bounded timeouts. Preserve
   existing B2 limits; allow up to 12 hours for local full-data operations, with
   systemd's deadline above the runner deadline and subprocess termination handled
   for the whole process group. Do not let the current 1-hour Restic / 110-minute
   runner limits silently truncate a full media backup or check.

A broken local URL/key, unavailable RustFS, missing local source or failed local
maintenance must not prevent B2 execution. A B2 outage or Healthchecks delivery
failure must not prevent local jobs from completing successfully.

## Direct capture, metadata and recovery compatibility

Continue copying small configuration files with stable-copy checks and exporting
PG/MariaDB/SQLite using existing consistency mechanisms. Restic reads additional
large allowlisted file roots directly alongside the job's staged export/manifest.
Do not create full media copies, hard links into live directories or privileged
bind mounts to simulate a staging layout.

DB-only jobs retain manifest schema v2. Full-service snapshots use schema v3:

- Staged artifacts retain source-ID-relative paths and explicit SHA-256 hashes.
- Direct file sources declare a validated absolute snapshot root and a source-ID-
  relative artifact inventory. Snapshot roots describe where bytes exist inside
  that snapshot; they never determine replacement-host destination paths.
- Direct-file integrity is marked `restic_authenticated_content`; do not invent
  SHA-256 values from a live pre-scan and claim they describe the archived bytes.
  Null file hashes are allowed only for explicitly typed v3 direct artifacts.
  Recovery must complete Restic `restore --verify` before using them.
- Record sizes and a pre/post source inventory to detect ordinary concurrent
  changes. Source errors, detected mutations and Restic partial-backup exit 3 fail
  the full-service job. Mark the capture as non-atomic even when no change is
  detected. Database dumps and media are not a transactional cross-service point
  in time; acceptance includes DB/media reference checks.
- Find the manifest at the exact scoped staging path, not by matching every file
  named `manifest.json` anywhere in a media tree.
- Keep image tags/digests/platforms and deployment/Vault revision metadata. Compute
  inclusion/exclusion statements from actual job scope instead of claiming all
  photos/attachments/downloads are excluded from the media job.

Extend the separate recovery role to accept v1/v2/v3, map approved direct sources
into fresh destinations and avoid duplicate narrow/full file mappings. Directory
ancestors may be shared with an explicitly excluded nested database destination
only when the registry authorizes both sources, ownership is compatible, and no
file artifacts collide. Do not broadly disable overlap checks.
Validate all paths, reject traversal/symlink escapes and prevent arbitrary source
snapshot paths from becoming target paths. Preserve current rules for empty
replacement storage, explicit activation, ownership and secret compatibility.
Aria2 session absence is not a recovery blocker for new snapshots.

A v2 DB-only restore still reports missing external media accurately; a v3 full
snapshot must restore the media it claims to protect. Recovery does not belong in
`site.yml`, and normal deployment must not silently overwrite restored files.

## Acceptance and regression requirements

- Run real independent runner processes against disposable repositories: hold a
  local media operation open and prove B2-like backup can complete concurrently;
  same-repository maintenance/backup conflict safely rather than racing.
- Inject unreachable/wrong-password/access-denied local destinations and missing
  local sources; prove the unaffected job succeeds with its own snapshot. Repeat
  with failed B2 transport/monitoring and successful local jobs.
- Render/deploy local jobs when Vault has only `jobs.database`; assert zero HTTP
  monitoring calls locally and preserve existing B2 failure/recovery behavior.
- Back up synthetic direct file trees without creating staged media copies; check
  missing roots, exclusions, SQLite WAL handling, partial uploads, source mutation,
  symlinks/traversal and sensitive-log handling.
- Use real Restic snapshots to restore v3 DB exports plus direct files and compare
  synthetic source/restore hashes. Exercise filenames containing `manifest.json`
  outside the authoritative manifest directory.
- Keep existing v1/v2 recovery fixtures passing. Test source coverage, n8n key
  preservation, WordPress/Pinchflat mapping replacement, ownership and Aria2
  recovery without session state.
- Ansible lint, syntax checks, unit/local integration tests, and Molecule idempotency
  must pass. Use existing container acceptance; no VM validation is required.
- Check timers, config permissions, independent service commands, first real
  snapshots and scratch restore after the user starts the authorized deployment.
  Do not call a timer deployment proof that the first real backup has succeeded.

## Delivery and operational boundaries

Keep the user's encrypted Vault edits intact; never print or commit plaintext.
Review/commit only intended files. Do not initialize production repositories,
start full backups, stop applications, prune old B2 data or deploy live services
as a side effect of writing the implementation.

The implementation handoff must state the exact commands for separate Ansible
configuration, explicitly selected initialization, independent manual job starts,
status inspection and scratch restores. Deployments requiring sudo remain user-
initiated because the agent has no become password.

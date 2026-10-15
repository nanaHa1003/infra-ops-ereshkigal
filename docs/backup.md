# Database and configuration backups

This job protects the active services' databases and selected configuration.
It does **not** protect photos, videos, downloaded files or all application attachments.
An encrypted off-site snapshot is not considered operationally verified until its databases have been restored in isolation.

## Deployment and acceptance status (2026-10-15)

The current host has enabled backup, maintenance and check timers. The first B2
backup succeeded; Healthchecks delivered failure and recovery emails. A scratch
restore passed restic's content verification. Mealie and Pinchflat passed SQLite
integrity checks. Isolated PostgreSQL imports succeeded for n8n (0 workflows,
1 credential) and Authentik (9 users, 5 applications, 5 providers, 20 flows),
matching the known production baselines.

Still pending: WordPress and Immich database imports, n8n credential decryption,
application login/recovery checks, a deliberately missed-ping alert, and observation
of a scheduled backup. Media protection, especially original Immich photos/videos,
remains a separate next phase and is not provided by this job. Protected restore
scratch data remains on the host until acceptance and deliberate cleanup.

## Configuration

Run `backup.yml` separately from `site.yml`. Repository initialization is opt-in; backup jobs run through enabled timers or an explicit service start.
Timers default to disabled. `backup_repositories` contains public URLs; `backup_jobs` associates each job with a repository, an explicit source list and optional schedule/retention overrides.
The current ereshkigal host explicitly overrides `backup_timers_enabled: true` at the user's request.
Applying `backup.yml` to this host enables its timers; override with `-e backup_timers_enabled=false` when only deploying inactive packaging.
Different jobs can use different buckets or S3-compatible providers. Repository and job names are independent of object prefixes.

Current destination:

```text
s3:https://s3.us-west-004.backblazeb2.com/homelab-db-snapshots/ereshkigal
```

The B2 Application Key is restricted to this private bucket and the `ereshkigal/` prefix.
Keep Object Lock disabled for this initial design. Do not add lifecycle rules which delete live restic repository objects.
B2 can retain hidden/noncurrent versions after deletion; review their lifecycle and billed usage separately from restic's logical repository size.

Edit secrets with `ansible-vault edit inventory/group_vars/all/vault.yml`:

```yaml
vault_secrets:
  backup:
    repositories:
      database:
        access_key_id: "B2 keyID"
        secret_access_key: "B2 applicationKey"
        restic_password: "independently generated random password"
    jobs:
      database:
        healthchecks_ping_url: "https://hc-ping.com/<check UUID>"
```

These are example field names, not values to replace existing secrets with.
Retain the original `vault_secrets.aria2.rpc_secret`, which the config template uses for `aria2.saveSession`.
Never paste secret values in chat, use them in command arguments, or commit decrypted Vault/config files.
After repository initialization, merely editing `restic_password` does not rotate the repository key: use restic's key management procedure and verify access before removing any old key.

## What is included

The host inventory is the authoritative allowlist. Verified source paths on the current deployment:

| Service | Export / configuration |
| --- | --- |
| Authentik | `auth-postgresql-1`, persisted custom templates, Compose |
| n8n | `n8n-n8n-postgres-1`, `/mnt/storage/n8n/data/config` including encryption key, Compose |
| Immich | `immich_postgres`, database image/extension metadata, Compose |
| WordPress | `wordpress-mariadb-1`, `wp-config.php`, themes/plugins, optional mu-plugins and `.htaccess`, Compose |
| Mealie | `/mnt/storage/mealie/data/mealie.db`, custom prompts, Compose |
| Pinchflat | `/mnt/storage/pinchflat/config/db/pinchflat.db`, extras including cookies, Compose |
| Homepage | persisted config, Compose |
| Aria2 | config/settings/scripts/filter, session explicitly flushed through RPC and copied with change detection, Compose |
| Proxy/DDNS | both proxy Compose manifests, Caddyfile, Caddy data/config including TLS private keys, root-only DDNS script |

PostgreSQL dumps use each running database container's matching `pg_dump`, custom archive format and no ownership/ACL commands.
The manifest records database/user/version/extensions and image reference. Use the backed-up Compose and off-host Vault to recreate the original application role and grants before a real recovery.
MariaDB dumps include database creation, routines, events and triggers. Only InnoDB base tables are accepted; a nontransactional table fails the job rather than claiming a consistent backup.
The current WordPress schema was checked read-only: InnoDB, and its configured user can export schema with routines/events/triggers.
Mealie and Pinchflat use Python's SQLite online backup API and integrity checking; Pinchflat has an active WAL, which a plain file copy could miss.

Files are copied from explicit roots, with symlinks rejected and change detection during each copy.
The job is not a cross-service or filesystem-wide point-in-time snapshot. Avoid plugin/theme updates or configuration deployments during a backup.
All required sources must succeed before upload. Missing optional sources are recorded explicitly in the manifest.

Aria2's `superng6/aria2` image reads the RPC token from `SECRET`, which overrides
`rpc-secret` in its configuration file. The Compose template passes the Vault token
through that variable so the backup's `aria2.saveSession` request uses the same token.
After updating an older deployment that used `RPC_SECRET`, deploy the `aria2` tag
before retrying a backup; the container is recreated with its existing config and downloads.

Excluded: Immich original media/ML cache, WordPress uploads, Authentik media images, Mealie images/attachments, n8n external binary files/logs, Pinchflat downloads/metadata media, Aria2 downloads/DHT cache, Redis/Valkey caches, raw PostgreSQL/MariaDB volumes, retired New-API, LANraragi and Home Assistant.
Database-only Immich restore cannot recover lost photos. The same limitation applies to other excluded attachments.

## Initial deployment, one step at a time

1. Save an independent recovery record in a password manager/offline location: repository URL, restic password, B2 access/recovery details, and Vault unlock method.
   Also save the controller checkout including uncommitted configuration and encrypted Vault separately off-host. The server job cannot back up controller-only files, SSH access or the sole Vault unlock key.
2. Deploy packaging with timers off, then repeat to verify `changed=0`:

   ```bash
   ANSIBLE_HOST_KEY_CHECKING=True ansible-playbook backup.yml \
     -e backup_timers_enabled=false --ask-become-pass
   ```

   This installs dependencies, protected runtime files and inactive timer units. Required source path validation happens without exporting data or connecting to B2.
3. Explicitly request initialization through Ansible (this also deploys updated Vault credentials):

   ```bash
   ANSIBLE_HOST_KEY_CHECKING=True ansible-playbook backup.yml \
     -e backup_initialize_repositories=true -e backup_timers_enabled=false --ask-become-pass
   ```

   Initialization defaults to off. An accessible repository is unchanged; only restic's explicit repository-missing exit 10 permits initialization.
   Wrong-password, permission and other errors fail without initialization or replacement. Older restic versions with ambiguous missing-repository exit 1 fail closed and require an explicit one-time `init` command.
   Direct server initialization remains available with `sudo /usr/local/libexec/homelab-backup/backup_runner.py init database`.
   For S3 AccessDenied, verify the key's bucket and prefix exactly match the URL; for example `ereshkiagl/` and `ereshkigal/` are different scopes.
4. Run the first backup and view its status:

   ```bash
   sudo systemctl start homelab-backup@database.service
   sudo journalctl -u homelab-backup@database.service -n 60 --no-pager
   sudo /usr/local/libexec/homelab-backup/backup_runner.py snapshots database
   ```

   Expect `Backup uploaded`, a new snapshot, and success in Healthchecks. Exit 2 means upload succeeded but some monitoring delivery failed; inspect Healthchecks/network connectivity before treating monitoring as healthy.
   All nonzero restic exits, including partial-backup exit 3, fail the job.
   Uploads initially have a pending tag. Only an exit-0 upload is promoted to the completed job tag;
   partial snapshots are excluded from the normal list and `latest` restore selection.
5. Read the repository and restore to protected scratch storage:

   ```bash
   sudo /usr/local/libexec/homelab-backup/backup_runner.py check database --full
   sudo /usr/local/libexec/homelab-backup/backup_runner.py restore database --snapshot latest
   ```

   The restore command creates a new directory under `/var/lib/homelab-backup/restores/`, prints its path, and verifies restored file contents. It never restores to original application paths.
   Restic retains the staging tree's absolute directory structure beneath this target; locate `manifest.json` and source-ID directories there.
6. Perform the database restore checks below. Test Healthchecks failure/recovery notifications and a deliberately missed ping after a real first backup; do not fabricate a success ping to make an untested backup look green.
7. Only after the first backup/restore verification, enable scheduling:

   ```bash
   ANSIBLE_HOST_KEY_CHECKING=True ansible-playbook backup.yml \
     -e backup_timers_enabled=true --ask-become-pass
   ```

   Persist `backup_timers_enabled: true` in host vars after acceptance; otherwise a later ordinary backup play intentionally disables timers again.
   Check `systemctl list-timers 'homelab-backup*'` and verify a fresh snapshot after the first scheduled run.

## Database restore checks

Use an isolated disposable Docker host or containers with `--network none`, no published ports, no production mounts and separate temporary database storage.
Never start the backed-up full Compose projects for this test: applications could run workflows, send mail or access production services.
Record commands, image versions, sample counts and duration without recording credentials or application data.

- PostgreSQL: use the database image/version from the manifest (currently Authentik PG16, n8n PG18, Immich PG14 with VectorChord/pgvecto.rs).
  Create a fresh scratch database and load `database.dump` with `pg_restore --exit-on-error --no-owner --no-acl` via stdin.
  Use matching extension libraries and preload settings for Immich; a generic postgres image is insufficient.
  Query application tables and compare representative counts, including n8n workflows/credentials, against the source baseline.
  Ownership is intentionally absent from dumps: in real recovery import as the restored application owner, or explicitly assign grants afterward.
- MariaDB: create a scratch server using the recorded image/version, import `database.sql` with the MariaDB client and abort on SQL errors.
  Recreate referenced routine/view/event DEFINER accounts or map definers only in the scratch copy if required; preserve the original dump.
  Verify WordPress tables and representative row counts. Production DB credentials/grants come from off-host Vault, not system tables in this dump.
- SQLite: run `PRAGMA integrity_check` against each restored `database.sqlite`; expect one `ok` result. Query representative recipe/source records.
- n8n encryption: retain the restored `.n8n/config` paired with its DB export. A matching key file's presence alone does not prove credential decryption.
  Verify decryption in an isolated, version-matched n8n process with no production networking and workflows inactive; do not emit credential plaintext into logs.
- Restore file owners/modes and SELinux labels on a real recovery using the intended application roles. Export files are deliberately root-only, rather than original application permissions.

Only mark these checks complete after executing them. Local automated tests prove a restic/SQLite round trip and orchestration failure behavior; they cannot prove the production datasets restore correctly.
Protected restore scratch files contain secrets and require deliberate cleanup after inspection; do not leave them on a shared filesystem.

## Scheduling, retention and monitoring

- Backups: 00/06/12/18 Asia/Taipei; persistent systemd timers catch a missed run after restart.
- Weekly retention/metadata check: Sunday 03:00 Asia/Taipei. Monthly rotating quarter-data check: day 1 at 09:00 Asia/Taipei.
- Every operation shares a nonblocking lock outside staging. An overlapping operation fails without deleting the active staging tree or uploading another batch.
- Retention is a union: `--keep-within 48h --keep-daily 14 --keep-weekly 8 --keep-monthly 12`, filtered by fixed host, job tag and staging path.
  `keep-within` is relative to the latest snapshot. Sparse daily/weekly/monthly versions may span longer calendar periods.
- Pending snapshots from unsuccessful uploads use a separate scoped retention (48h plus the most recent pending snapshot for diagnosis). They are never reported as verified backup versions.
- Preview with `sudo /usr/local/libexec/homelab-backup/backup_runner.py retention-preview database` before manual maintenance. Preview prints kept/removed snapshot IDs and never prunes.
- Healthchecks: Simple period 6h, grace 2h. Start expects completion within 2h; no ping for roughly 8h also alerts. Explicit failure signals alert sooner.
- Only a complete export and successful upload send success. Healthchecks receives no SQL, secrets or raw command output.
- Maintenance does not send backup success pings, since that could hide stalled backups. Its failures are visible in systemd/journal; external maintenance monitoring needs a separate Check and is not configured by this version.
- `TimeoutStartSec=115min` and a runner deadline prevent operations from hanging indefinitely. No guarantee of a recovery time is made before measuring a full restore.

Review runtime, transferred bytes, B2 billed usage and retained versions after one week. Six hours is a target recovery point plus backup completion time, contingent on successful jobs; the eight-hour alert threshold is not a data-loss guarantee.

## References

- [restic repository setup and B2 S3 API](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html)
- [restic retention semantics](https://restic.readthedocs.io/en/stable/060_forget.html)
- [SQLite online backup](https://www.sqlite.org/backup.html)
- [Immich backup and restore](https://docs.immich.app/administration/backup-and-restore/)
- [Healthchecks scheduling](https://healthchecks.io/docs/configuring_checks/)

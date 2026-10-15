# Fedora43 x86_64 acceptance — pending external test VM

This is a manual acceptance gate, not an executed result. Never substitute the
production host or claim that the Fedora40 Molecule container proves SELinux.
Use an empty, disposable Fedora43 x86_64 VM, dedicated mounted test disk, enforcing
SELinux, SSH/sudo and no production network/DNS/B2 credentials. The controller
must use only synthetic Vault fields and `.example` domains.

1. Export the synthetic Authentik/n8n/Immich/MariaDB/SQLite fixtures to a **local**
   Restic repository using the same helpers as the real engine matrix. Capture
   image digests/versions/schema metadata. Remove all source containers and source
   data. Do v1 (explicit legacy profile) and v2 runs separately.
2. Copy that repository to the VM. Create a dedicated inventory using the example;
   set a new host name, operator, storage root and stack root. Set `domain` to the
   synthetic source domain; never use production Vault, a B2 URL or real ping URL.
3. Follow `docs/recovery.md`: plan, restore, verify, verify again. Run
   `verify.yml` with `recovery_vm_disposable=true` and the synthetic profile/run ID.
   Inspect actual `getenforce`, `ls -Zd` and `docker inspect` selected fields:
   no ports/host networking/socket on validation DBs, all stopped after verify,
   run labels match, restored file owners and PG18 parent mount correct.
4. Reboot the VM; verify again. Confirm no app containers, DDNS cron, backup timers,
   notifications or outbound fake SMTP/webhook/DNS requests started. Repeat with
   different supported file UID/GID mappings and storage paths in a new run.
5. Inject missing mount, low space, changed profile, failed SQL and SIGTERM;
   require failure without overwrite. Preview and perform exact run-owned cleanup
   before activation; ensure unknown files/containers cannot be removed.
6. In this isolated fixture only, fence/remove the source, explicitly activate
   supported services in dependency order and validate DB + app readiness. Withhold
   media, ensure affected services stay stopped; confirm other selected services
   can start. Simulate first ready failure: no later services may start and cleanup
   must remain blocked. Record awaiting_cutover, then manually confirm fake cutover
   and rerun all started services. No real DNS or DDNS activation is needed.
7. Save versions, snapshot IDs, phase timing, `du`/free-space high-water marks,
   reboot observations, and redacted assertions in a dated acceptance report.
   Do not record dumps, passwords, decrypted credentials or raw inspect Env.

Until this passes, recovery is development-tested only; there is no verified
production RTO. The media backup/restore procedure is outside this acceptance.

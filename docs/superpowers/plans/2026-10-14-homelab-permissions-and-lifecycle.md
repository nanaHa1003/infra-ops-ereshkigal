# Homelab permissions and lifecycle fixes

> **For agentic workers:** Use superpowers:executing-plans to implement this plan inline.

**Goal:** Protect rendered credentials, reliably reconcile the selected services, and retire New-API without deleting data.

**Architecture:** Keep the existing per-service roles. Let site.yml own ordering and service selection, and down.yml own teardown without invoking deployment dependencies. Reconcile Compose on every deployment; explicitly recreate Caddy when its bind-mounted configuration changes.

**Tech Stack:** Ansible, Docker Compose, Jinja2, Python unittest, Molecule.

**Spec:** User-authorized fixes from the repository review in this conversation; New-API shutdown explicitly requested, with database volume and secrets preserved.

## Global constraints

- No production secrets in test output or fixtures; no decryption for local checks.
- No database upgrades, migrations, volume deletion, or full live deployment.
- Preserve existing image versions and the installed socket-proxy project identity.
- Work on fix/homelab-permissions-and-lifecycle; leave changes reviewable and uncommitted.
- Live actions are limited to securing the deSEC script and stopping New-API.

## Review focus

- Unchanged templates must still recover stopped services.
- String extra-vars must not turn false into a forced restart.
- Disabled services and teardown must not deploy dependencies.
- Shared paths must remain accessible to both containers under SELinux.
- Secret diffs and retired-service data must remain protected.

## Tasks

- [x] Add regression checks for unchanged-file reconciliation, force-restart coercion, secret permissions/diffs, role tag selection, and shared mounts; observe failures first.
- [x] Secure the DDNS script and secret-bearing templates; reconcile Compose unconditionally with explicit pull/recreation controls; recreate Caddy on config changes.
- [x] Wire enabled_services and role tags, make teardown independent of deployment dependencies, and correct shared SELinux labels.
- [x] Remove New-API deployment code and directory declarations. Stop its live containers with restart disabled, retaining data and secrets.
- [x] Update README, extend CI/Molecule checks, run local lint/syntax/Compose/regression checks and Molecule where available, then review the complete diff.

## Progress

- Baseline: clean checkout; known lint failure in New-API's changed-only deployment task. Previous review reproduced empty --tags n8n task selection.
- Live host: SSH and Docker access available; sudo requires an interactive password. User asked to run the narrow chmod command directly.
- Live verification: DDNS script is root:root 0700. All three New-API containers are exited with restart=no; new-api_pg_data still exists. No files, containers, volumes, or Vault keys were removed on the host.
- Initial six contract tests failed with 52 subtest failures before implementation, then passed. Three additional checks exercise real Ansible selection, disabled-service skipping, and teardown task selection.
- Validation: production-profile lint passed (78 files); site/down syntax checks passed; 10 active mock-rendered Compose files passed docker-compose config. Invalid prerequisite selection failed with changed=0 as intended.
- Environment: sandboxed Ansible execution cannot start its local RPC socket; the same regression checks pass with escalation. Molecule needs VM startup and test execution in the same command to keep the local Podman connection available.
- Independent review: no Critical/Important findings. Minor (deferred): regression coverage checks Caddy's shared WordPress mount, but should also assert the WordPress and Authentik sides. Those templates were inspected and are correct.
- Scope: database migration/backup setup, socket-proxy project migration and privileges, and full live application/SELinux validation remain separate work.
- Final results: 9/9 regression tests passed. Full Molecule test cycle passed, including idempotence (26 tasks, changed=0), root-only file assertions and test-container cleanup. Syntax, production lint, all 10 active Compose configurations and git diff --check passed. Changes remain uncommitted on the dedicated branch.
- 2026-10-15: User's real n8n dry-run passed (ok=6, changed=1, failed=0); only the Compose template reported a predicted change. This does not establish whether the difference is content or permissions.
- 2026-10-15: User confirmed OpenVINO previously failed and CPU inference is the working configuration. Changed Immich ML's default image to v2.7.5 CPU, disabled this host's hardware passthrough, and made optional device/USB fields conditional. CPU tests first reproduced the old OpenVINO default and invalid empty USB mount, then passed after the fix. Real deployment will remove residual GPU/USB access and can recreate the ML container.
- CPU follow-up verification: 12/12 regression tests passed; production lint and site/down syntax passed; Compose validation passed for default CPU, host CPU and explicit hardware overrides. Full Molecule cycle passed (second converge changed=0, verify failed=0, test instance destroyed). Independent review found no Critical/Important issues. No live deployment was performed.
- n8n live deployment: user ran the tagged deployment twice (first changed=1 for the template, second changed=0; both failed=0). Post-deployment read-only checks confirmed n8n and PostgreSQL running with their original start times and restarts=0, PostgreSQL healthy at 18.4, health/readiness and public HTTPS all 200, and expected data counts unchanged (0 workflows, 1 credential). n8n is now deployed and idempotency verified; other services remain pending.
- Immich deployment warning: named model-cache incorrectly used the bind-only :Z option. A new regression test reproduced Compose normalizing that mount into type=volume with bind.selinux=Z. Removed :Z from the named-volume mount; 13/13 tests passed, including normalized Compose validation. Read-only live checks show all four Immich containers healthy, HTTPS 200, CPU ML image active and immich_model-cache preserved. User must rerun the tagged deployment to apply this warning fix and then verify changed=0.
- User reported the Immich rerun completed. Post-deployment inspection confirms all four containers running/healthy with restarts=0, generic v2.7.5 CPU ML, devices/cgroup rules absent, cache mounted as immich_model-cache:/cache:rw without :Z, and public HTTPS 200. The final Ansible recap was not provided, so its changed count is not independently recorded.
- User subsequently provided the Immich recap: ok=6 changed=0 failed=0, confirming live idempotency.
- Full-site dry-run: ok=45 changed=10 failed=0 skipped=1. No live full-site deployment occurred. It exposed the pre-existing split proxy project and legacy network ownership warnings: homelab_backend belongs to project homelab, while all three proxy containers belong to proxy.
- Proxy follow-up: keep existing proxy identity and file paths; every Compose operation loads both manifests, and each up operation targets only its own services. Ensure shared networks using docker_network without forcing replacement or changing legacy labels/endpoints; declare networks external to Compose and preserve them on teardown. Added requests to bootstrap dependencies; current host already has requests 2.33.1. Regression tests reproduced incomplete models, unscoped service selection, and non-external networks before the fix.
- Proxy verification: 16/16 regression tests passed; production lint (42 files), site/down syntax and git diff --check passed. Actual network tasks in check mode reported changed=0. Target Compose 5.6 dry-run against the newly rendered complete project exited successfully without network/orphan warnings. Read-only inspection afterward confirmed the three live proxy containers retain their original IDs and October 14 start times. Independent review found no Critical/Important issues. No live proxy deployment or repeat full Molecule cycle was performed for this follow-up. Ansible check mode can still read the old on-disk Compose files until the templates are actually deployed.
- Proxy live verification: user supplied the tagged deployment recap ok=11 changed=0 failed=0 with no network/orphan warnings. Post-deployment read-only checks show Caddy and both socket proxies running with restarts=0; Caddy's current start time is October 15 06:17:58 UTC, while both socket proxies retain their October 14 start times. The public n8n route through Caddy returns HTTPS 200. Proxy convergence is verified; full-site live deployment remains pending.
- Authentik follow-up: user supplied ok=6 changed=2 failed=0 (Compose template and reconcile changed). Read-only inspection found server/worker recently started and healthy on 2026.5.2; PostgreSQL 16 and Redis remain healthy with approximately 23 hours uptime. Public /-/health/ready/ returns HTTPS 200. The supplied excerpt omits the invocation flags; a second actual tagged run with changed=0 is still needed to establish idempotency.
- Authentik idempotency: user subsequently supplied the actual tagged command and recap ok=6 changed=0 failed=0.
- Remaining application batch: user reported completion and recap ok=19 changed=0 failed=0 for wordpress,pinchflat,mealie,homepage,aria2. Read-only inspection confirms all five applications and WordPress MariaDB running; Pinchflat, Mealie and Homepage expose healthy status. Public WordPress and Mealie return 200, Pinchflat returns 401, and Homepage returns 302. These checks establish availability/authentication responses, not authenticated UI functionality. All active application roles now have user-reported live convergence; common/system configuration and backup implementation remain pending.

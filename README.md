# infra-ops-ereshkigal

**Infrastructure as Code (IaC) for the Ereshkigal Home Lab.**

This repository manages the deployment, configuration, and orchestration of a
multi-service home lab environment. It is built to be reproducible, secure, and
optimized for **Fedora 43** running on a Netbird mesh network.

## Architecture Overview

| Concern | Tool |
|---|---|
| Orchestration | Ansible (local-to-remote push model) |
| Container engine | Docker CE (replacing Podman for consistent UID/GID mapping) |
| Reverse proxy | Caddy with deSEC DNS-01 TLS |
| Identity provider | Authentik (SSO / OIDC / LDAP) |
| Networking | Netbird mesh VPN |
| DNS / DDNS | deSEC (dynamic DNS, updated every 15 min via cron) |
| Secrets | Ansible Vault (AES-256) |

---

## Project Structure

```
.
├── ansible.cfg                        # Ansible settings (inventory path, pipelining, vault)
├── site.yml                           # Master deploy playbook
├── down.yml                           # Graceful teardown playbook (reverse order)
│
├── inventory/
│   ├── hosts.ini                      # Real inventory — gitignored, create from .example
│   ├── hosts.ini.example              # Inventory template to commit
│   ├── group_vars/
│   │   ├── all/
│   │   │   ├── vars.yml               # Global: domain, timezone, maintainer
│   │   │   └── vault.yml             # All secrets (AES-256 encrypted)
│   │   └── homelab/
│   │       └── vars.yml               # Group: stack_root, storage_root,
│   │                                  #        storage_permissions, enabled_services
│   └── host_vars/
│       └── ereshkigal.netbird.cloud/
│           └── vars.yml               # Host-specific: GPU devices, Python interpreter
│
├── roles/
│   ├── common/          # OS baseline: Docker CE, SELinux, storage dirs, DDNS cron
│   ├── proxy/           # Caddy reverse proxy + Docker socket proxies
│   ├── auth/            # Authentik (PostgreSQL + Redis + server + worker)
│   ├── wordpress/       # WordPress FPM + MariaDB
│   ├── pinchflat/       # Pinchflat YouTube media manager
│   ├── tubearchivist/   # TubeArchivist (disabled — kept for reference)
│   ├── immich/          # Immich + ML (OpenVINO) + PostgreSQL + Valkey
│   ├── mealie/          # Mealie recipe manager + AI prompts
│   ├── homepage/        # Homepage dashboard
│   ├── n8n/             # n8n automation + PostgreSQL
│   └── aria2/           # Aria2 download daemon
│
├── molecule/
│   └── default/         # Molecule test scenario (Podman backend)
│       ├── molecule.yml
│       ├── prepare.yml
│       ├── converge.yml
│       └── verify.yml
│
└── .github/
    └── workflows/
        └── ci.yml       # GitHub Actions: lint + molecule on push/PR
```

Each role follows a consistent structure:

```
roles/<name>/
├── defaults/main.yml    # Declared variable interface (image tags, ports, limits)
├── meta/main.yml        # Role metadata and declared dependencies
├── tasks/
│   ├── main.yml         # Deploy tasks
│   └── down.yml         # Teardown tasks
└── templates/
    └── docker-compose.yml.j2
```

---

## Services

| Service | Role | URL |
|---|---|---|
| Caddy reverse proxy | `proxy` | — |
| Docker socket proxies | `proxy` | — |
| Authentik SSO | `auth` | `auth.ereshkigal.dedyn.io` |
| WordPress blog | `wordpress` | `blog.ereshkigal.dedyn.io` |
| Pinchflat YouTube manager | `pinchflat` | `tube.ereshkigal.dedyn.io` |
| Immich photos | `immich` | `photo.ereshkigal.dedyn.io` |
| Mealie recipes | `mealie` | `cook.ereshkigal.dedyn.io` |
| Homepage dashboard | `homepage` | `ereshkigal.dedyn.io` |
| n8n automation | `n8n` | `auto.ereshkigal.dedyn.io` |
| Aria2 downloads | `aria2` | RPC on port 6800 |

---

## Prerequisites

- Ansible installed in the `ansible` conda environment
- `.vault_pass` file present at the repo root (gitignored)
- `inventory/hosts.ini` created from `inventory/hosts.ini.example`

---

## Developer Setup

Install the pre-commit hook to catch credential leaks locally before they
reach CI. This is a one-time step after cloning the repo.

```bash
pip install pre-commit   # or: brew install pre-commit
pre-commit install
```

From that point on, gitleaks runs automatically on every `git commit`,
scanning the staged diff against the rules in `.gitleaks.toml`.

**Run manually against all files:**

```bash
pre-commit run --all-files
```

**Skip the hook for a single commit** (e.g. after deliberately revoking a
leaked credential before the rotation is complete):

```bash
SKIP=gitleaks git commit -m "message"
```

---

## Usage

### Deploy

```bash
conda activate ansible
ansible-playbook site.yml --ask-become-pass
```

To force-restart a specific service without changing any files:

```bash
ansible-playbook site.yml --ask-become-pass -e "force_restart=true" --tags <role>
```

### Tear down

Stops all stacks in reverse dependency order (apps → auth → proxy):

```bash
ansible-playbook down.yml --ask-become-pass
```

### Dry run

Preview changes without applying them:

```bash
ansible-playbook site.yml --check --diff --ask-become-pass
```

---

## Secret Management

All secrets live in `inventory/group_vars/all/vault.yml` under a single
`vault_secrets` dictionary, keyed by service name.

```bash
# Edit secrets
ansible-vault edit inventory/group_vars/all/vault.yml

# View secrets
ansible-vault view inventory/group_vars/all/vault.yml
```

See the [vault structure](#vault-structure) section below for the full list of
expected keys.

### Vault Structure

```yaml
vault_secrets:
  desec:         { login, api_token }
  authentik:     { pgpass, secret_key, email_username, email_password }
  wordpress:     { mysql_database, mysql_user, mysql_password }
  immich:        { db_database_name, db_username, db_password }
  mealie:        { openai_base_url, openai_api_key,
                   oidc_provider_name, oidc_configuration_url,
                   oidc_client_id, oidc_client_secret,
                   oidc_user_group, oidc_admin_group }
  n8n:           { postgres_user, postgres_db, postgres_password }
  aria2:         { rpc_secret }
  pinchflat:     { username, password }
```

---

## Adding a New Service

1. Create a new role: `mkdir -p roles/<name>/{defaults,meta,tasks,templates}`
2. Add `defaults/main.yml` — declare the image tag and any tunable variables
3. Add `meta/main.yml` — set `dependencies: [proxy]` (or `[proxy, auth]` if OIDC is needed)
4. Add `tasks/main.yml` — create stack dir, template compose file, start stack (tag the `docker_compose_v2` task with `molecule-notest`)
5. Add `tasks/down.yml` — stat + `docker_compose_v2 state: absent`
6. Add `templates/docker-compose.yml.j2` — use `homelab_backend` external network
7. Add any new secrets to `inventory/group_vars/all/vault.yml`
8. Add storage directories to `inventory/group_vars/homelab/vars.yml` under `storage_permissions`
9. Add the role name to `site.yml` and `down.yml`
10. Run `molecule test` to verify templating and idempotency

---

## Running Tests

Tests use [Molecule](https://ansible.readthedocs.io/projects/molecule/) with a
Podman backend. They verify template rendering, directory creation, and
idempotency — without requiring a live Docker daemon inside the test container.

### Setup (one-time)

```bash
conda activate ansible

# Expose the Podman socket so Molecule can find it
export DOCKER_HOST=$(podman machine inspect --format '{{.ConnectionInfo.PodmanSocket.Path}}')

# Symlink podman as docker in the conda env (required by community.docker collection)
ln -sf /opt/podman/bin/podman ~/.miniconda3/envs/ansible/bin/docker
```

### Run

```bash
molecule test          # Full cycle: create → converge → idempotence → verify → destroy
molecule converge      # Apply roles only (container persists)
molecule verify        # Run assertions only
molecule destroy       # Remove the test container
```

### What is tested

| Check | How |
|---|---|
| All Jinja2 templates render without errors | `converge` runs all template tasks |
| Storage directories created with correct ownership | `verify` checks with `stat` |
| All compose files land in correct paths | `verify` checks each `docker-compose.yml` |
| Homepage `PUID=1000` (not `GUID`) | `verify` greps rendered file |
| Mealie `OPENAI_MODEL` env var present | `verify` greps rendered file |
| n8n image is pinned to a version tag | `verify` greps rendered file |
| Authentik worker has Docker socket mount | `verify` greps rendered file |
| Immich connected to `homelab_backend` | `verify` greps rendered file |
| All roles are fully idempotent | `idempotence` runs converge twice; second run must show 0 changes |

---

## Fedora / SELinux Notes

- All volume mounts in Compose templates use `:Z` to allow Docker to relabel
  files for SELinux container access
- Package management uses `dnf5`
- The `common` role sets `container_file_t` context on `/mnt/storage` via
  `sefcontext` + `restorecon`

## Storage & UID Alignment

Storage directories are created by the `common` role in a single pass, using
the `storage_permissions` list defined in `inventory/group_vars/homelab/vars.yml`.
Each entry specifies the path (relative to `/mnt/storage`), owner UID, group GID,
and mode. UIDs are aligned to match container process UIDs:

| UID | Used by |
|---|---|
| 0 | Caddy, Immich library |
| 70 | Authentik PostgreSQL |
| 999 | MariaDB, n8n PostgreSQL, Immich PostgreSQL, Redis |
| 1000 | Most application data (Mealie, Homepage, n8n, Aria2, WordPress) |

---

## License

MIT — Created and maintained by nanaha1003.

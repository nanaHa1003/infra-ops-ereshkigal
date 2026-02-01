# infra-ops-ereshkigal

**Infrastructure as Code (IaC) for the Ereshkigal Home Lab.**

This repository manages the deployment, configuration, and orchestration of a multi-service home lab environment. It is built to be reproducible, secure, and optimized for **Fedora 43 Workstation** running on a Netbird mesh network.

## 🏛 Architecture Overview

* **Orchestrator:** [Ansible](https://www.ansible.com/) (Local-to-Remote push model).
* **Container Engine:** Docker Engine (replacing default Podman for consistent UID/GID mapping).
* **Network:** [Netbird](https://netbird.io/) mesh VPN for secure remote management.
* **Storage Root:** `/mnt/storage` with standardized UID/GID alignment.
* **Reverse Proxy:** Caddy (Transitioning to Traefik with Authentik LDAP support).

---

## 📂 Project Structure

```text
.
├── ansible.cfg            # Project-specific Ansible settings
├── inventory.ini          # Server definitions (Netbird addresses)
├── site.yml               # Master playbook entry point
├── group_vars/
│   └── all/
│       ├── vars.yml       # Public configuration (URLs, Paths)
│       └── vault.yml      # Encrypted secrets (AES-256)
├── roles/
│   ├── common/            # OS hardening, Docker install, UID alignment
│   └── apps/              # Docker Compose stack deployments
└── .vault_pass            # Local vault password (GIT IGNORED)

```

---

## 📝 Maintenance Memo (The "How-To")

### 1. Daily Management

To deploy changes or add new services, run the master playbook from your laptop:

```bash
ansible-playbook site.yml

```

### 2. Secret Management

Secrets are stored in `group_vars/all/vault.yml`. **Never commit plain-text passwords.**

* **Edit secrets:** `ansible-vault edit group_vars/all/vault.yml`
* **View secrets:** `ansible-vault view group_vars/all/vault.yml`

### 3. Fedora Specifics & SELinux

Because this lab runs on **Fedora**, SELinux is active.

* **Volumes:** Always append `:Z` to volume mounts in Compose templates (e.g., `- ./data:/app/data:Z`) to allow Docker to relabel files automatically.
* **Package Manager:** Tasks use `dnf5` for high-performance package management.

### 4. Storage & Permissions

The `common` role enforces a **"Great Alignment"** of UIDs to prevent permission drift across different database engines:

* **UID 1000:** Standard User (`nanaha`), Web files, Media.
* **UID 999:** MariaDB, Immich Postgres.
* **UID 70:** Authentik Postgres.

---

## 🚀 Current Stack Status

* [x] **Infrastructure:** Caddy, AdGuard Home.
* [x] **Identity:** Authentik (SSO & LDAP Outpost).
* [x] **Media:** Immich, TubeArchivist, LanRaragi.
* [x] **Productivity:** Mealie, WordPress.
* [x] **Downloads:** Aria2.

---

## 🛠 To-Do / Future Migration

* [ ] Migrate Caddy to Traefik for native Authentik integration.
* [ ] Implement automated backups for `/mnt/storage` using Rclone.
* [ ] Deploy a local LLM node using Ollama.

---

### License

MIT - Created and maintained by nanaha1003.

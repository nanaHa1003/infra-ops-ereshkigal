# Cloudflare DDNS implementation plan

Goal: deploy favonia/cloudflare-ddns for origin.ereshkigal.cc without changing service URLs.

Architecture: a dedicated non-root container checks public IPv4 every five minutes;
a scoped Cloudflare token is read from a protected bind-mounted file. No public
container ports, Docker socket access, WAF permissions, IPv6 updates, or deletion
on shutdown. Existing deSEC DDNS continues during migration.

- [x] Add role, pinned image, dedicated playbook and opt-in site.yml integration.
- [x] Validate Ansible syntax, lint and rendered Compose security properties.
- [ ] Create zone-scoped API token, save it in encrypted Vault, and deploy.
- [ ] Verify origin A record, DNS-only status, updater success and idempotency.

Live prerequisites: Cloudflare account access and passwordless become or a local
interactive deployment. Normal site deployment opts in with cloudflare_ddns_enabled.

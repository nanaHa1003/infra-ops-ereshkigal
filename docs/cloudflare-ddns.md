# Cloudflare DDNS

The dedicated playbook manages origin.ereshkigal.cc with
favonia/cloudflare-ddns. It creates a missing A record on its first successful
update and checks public IPv4 every five minutes. It leaves IPv6 records alone.
Do not leave a stale AAAA record for this hostname.

Create a Cloudflare API token with Zone / DNS / Edit, restricted to the
ereshkigal.cc zone. No WAF permissions or Global API Key are needed.
Store it using `ansible-vault edit inventory/group_vars/all/vault.yml`:

```yaml
vault_secrets:
  cloudflare:
    ddns_api_token: YOUR_SCOPED_TOKEN
```

Merge this namespace into the existing vault_secrets mapping. Do not replace it.
The token can edit DNS throughout that zone, not only this hostname. Use a
separate token for Caddy certificate issuance.

Deploy on the existing Docker host:

```sh
ANSIBLE_HOST_KEY_CHECKING=True ansible-playbook cloudflare-ddns.yml --ask-become-pass
```

Run twice to verify idempotency. The role is included in the host's
`enabled_services`, so ordinary `site.yml` runs reconcile it. Use
`--tags cloudflare_ddns` to run only this role through `site.yml`.

Verification on Ereshkigal:

```sh
sudo docker compose -f /opt/stack/cloudflare-ddns/docker-compose.yml logs --tail=50 updater
dig @1.1.1.1 origin.ereshkigal.cc A +short
```

Compare with the host's actual public IPv4. Confirm `proxied: false` in the
Cloudflare dashboard/API: PROXIED=false is only a creation/conflict fallback;
the updater preserves a consistent existing proxy status. Review an existing
origin record before starting the updater. A running container alone does not
prove DNS updates succeeded.

The token file is /opt/stack/cloudflare-ddns/secrets/api_token, root-owned and
group-readable only by the configured container GID. Compose contains no token.
Rotating the token through Ansible recreates the updater to reload the file.
Stopping the container retains DNS records. Existing deSEC DDNS and service
hostnames remain unchanged. DDNS credentials can be redeployed from Vault;
they are not automatically added to the current backup source list.

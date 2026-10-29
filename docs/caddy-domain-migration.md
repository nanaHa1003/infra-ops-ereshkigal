# Parallel Caddy domains

`domain` remains `ereshkigal.dedyn.io` for application configuration and deSEC
DDNS. `proxy_public_domain: ereshkigal.cc` enables seven additional Caddy sites:
auth, www (Homepage), blog, cook, tube, photo and auto. The apex is not served.
Removing this variable disables the new sites while retaining all legacy routes.

Legacy sites keep their deSEC DNS-01 issuer. New sites use Cloudflare DNS-01
through the custom image built in [images/caddy](../images/caddy/README.md).
No inbound port 80 is required for issuance or renewals. Keep Cloudflare in
Full (strict) mode. Port 443 must still reach Caddy for actual site traffic.

Create a separate Cloudflare token with Zone Read and DNS Edit for ereshkigal.cc
and place it in `vault_secrets.cloudflare.caddy_api_token`. The DDNS token remains
independent. The Caddy token is rendered into the root-only (0600) Caddyfile,
like the existing deSEC token; it is not put in the image or CI environment.
Ansible suppresses template output and diffs. Configuration backups therefore
contain both credentials and must remain encrypted and access-controlled.

Cloudflare edge CIDRs are trusted explicitly for forwarded client IPs. Direct
connections cannot select their client IP with forwarded headers. New proxy
routes send the parsed client IP upstream; WordPress receives it as REMOTE_ADDR.
Review the static CIDR list against Cloudflare's official IPv4/IPv6 lists when
maintaining this configuration.

This phase adds aliases, not application migration. Existing OIDC issuers and
callbacks, WordPress canonical URL, n8n webhook URL, and Mealie BASE_URL retain
their old values. Applications may redirect back to the old domain. Homepage's
new hostname uses the same Authentik protection, but its Authentik provider may
need adjustment before the new hostname works end-to-end. Immich retains its
existing OIDC configuration.

Legacy public aliases remain reachable by design. Consequently, enabling
Cloudflare Proxy on new names does not prevent access to the same applications
through the old names. Origin access restrictions should be a separate change
after deciding which legacy names remain public versus management-only.

Deploy after reviewing the candidate Caddy configuration:

First ensure the image referenced by `proxy_caddy_image` has been published and
is readable from Ereshkigal. Bootstrap candidates use immutable build tags; main
publishes the Caddy version tag. Pin the deployment reference by digest. Do not
deploy the old deSEC-only image with the Cloudflare DNS configuration.

```sh
ANSIBLE_HOST_KEY_CHECKING=True ansible-playbook site.yml --tags proxy --ask-become-pass
```

Then inspect Caddy certificate issuance logs and test HTTPS for all new and old
names, including photo/tube directly and other names through Cloudflare. Do not
redirect old names or remove their OIDC callbacks during this preparation phase.

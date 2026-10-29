# Caddy DNS image implementation plan

Goal: retain Caddy and both domains with DNS-01, using an image built in this repo.

Architecture: official Caddy builder/runtime plus pinned deSEC and Cloudflare
modules; native amd64/arm64 validation in CI, then GHCR publication. Renovate
tracks Caddy, plugins and base-image digests. Deployment remains explicit and
uses a registry digest so backup/recovery retain a retrievable image.

- [x] Add Dockerfile, isolated context and real image validation script.
- [x] Add multi-architecture build/test/publish workflow and Renovate rules.
- [x] Switch parallel-domain configuration to Cloudflare DNS-01 and add a blank
  independent Vault token field; never send credentials to CI.
- [x] Run local checks, push feature branch and verify the real CI build.
- [x] Publish initial image, verify anonymous pull availability, record digest
  and finish deployment instructions. Production cutover requires populated
  caddy_api_token and the user's interactive sudo deployment.

The first feature build published an immutable bootstrap candidate. The
temporary branch publication permission is now removed; only main can publish
further images or advance the version tag. No workflow runs Ansible against
the homelab.

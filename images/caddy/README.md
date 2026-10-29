# Caddy with Cloudflare and deSEC DNS providers

The Dockerfile pins official Caddy builder/runtime digests, the Caddy core
version and both module tags. Its isolated context excludes everything except
Dockerfile, including Vault, runtime Caddyfile and certificate data.

`Caddy image` builds and validates natively on GitHub-hosted amd64 and arm64
runners. Tests execute the real binary, check both modules and validate the
actual Ansible Caddyfile template in legacy-only and parallel-domain modes.
Validation uses synthetic tokens and no network, so it never issues certificates
or calls Cloudflare/deSEC APIs. After both architectures pass, it publishes a
multi-architecture image with SBOM and build provenance to:

`ghcr.io/nanaha1003/infra-ops-ereshkigal/caddy-dns`

Main builds publish a version tag (such as `2.11.7`) and an immutable build tag
(`2.11.7-build-RUN_ID-ATTEMPT`). Initial feature/cloudflare-ddns push builds may
publish only immutable build tags for bootstrap validation. PR workflows never
publish. There is no automated deployment to the homelab.

Verify package visibility before anonymous host/recovery pulls. The bootstrap
image has been verified anonymously readable. If future package settings make
it private, make this image-only package public in GitHub package settings or
configure registry authentication on deployment and recovery hosts. Runtime
secrets are not in this package; do not put a registry password in the image.

The bootstrap deployment reference pins the validated candidate's digest with
the intended Caddy version tag. Docker retrieves a tag+digest reference by
digest; the plain version tag is only published by main after merge.

Renovate groups official Caddy base-image and Caddy core updates. Module updates
are independent PRs. Base-image digest changes trigger rebuilds even when their
version tag is unchanged. A weekly workflow also validates and rebuilds the
currently pinned sources; it does not discover unmerged dependency updates.
The version tag may advance to a new digest; deployed digest references remain
unchanged until an explicit update and Ansible deployment. Keep old build tags
and digests for rollback and historical recovery; do not automatically purge them.

Build and test locally with Podman on ARM:

```sh
podman build --build-arg BUILDPLATFORM=linux/arm64 \
  --build-arg TARGETOS=linux --build-arg TARGETARCH=arm64 \
  -t localhost/homelab-caddy:test images/caddy
CONTAINER_ENGINE=podman python images/caddy/validate.py localhost/homelab-caddy:test
```

Use `amd64` instead on x86. With Docker Buildx, TARGETOS, TARGETARCH and
BUILDPLATFORM are set automatically. The validator requires Jinja2 and PyYAML.

Certificates and configuration use the existing /data and /config mounts. A
registry-pulled image supplies RepoDigests to the existing backup inventory.
When recovering during the alias migration, set `proxy_public_domain` explicitly
on the replacement inventory if both hostname sets should be rendered.

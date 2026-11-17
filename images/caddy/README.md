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

Main builds publish a version tag (such as `2.11.9`) and an immutable build tag
(`2.11.9-build-RUN_ID-ATTEMPT`). Only main can publish. PR workflows validate
without publishing. After publication, both native architectures pull and validate
the exact published digest before the workflow proposes it for deployment.

The workflow creates or updates a single PR from `automation/caddy-deployment`
to `main`, changing only `proxy_caddy_image` in `roles/proxy/defaults/main.yml`.
The PR contains the version, digest, source commit and build run link. New successful
builds update the same open PR, so it can wait for a convenient maintenance window.
Review and merge it manually, then run:

```sh
ansible-playbook site.yml -i inventory/hosts.ini --tags proxy
```

Merging a deployment pin does not trigger another image publication. A Caddyfile-only
change runs PR validation but does not publish another image on merge. Renovate
continues managing Caddy build dependencies; custom image deployment PRs are owned
by this workflow to avoid duplicate updates.

One-time GitHub setup: under Settings > Actions > General > Workflow permissions,
enable "Allow GitHub Actions to create and approve pull requests". The workflow uses
`GITHUB_TOKEN` with scoped job permissions; no additional PAT or app secret is needed.
It explicitly dispatches `ci.yml` on the generated branch because bot pushes do not
trigger push workflows. GitHub may also show approval-required PR workflow runs;
the explicitly dispatched CI runs independently. No automatic merge or server deploy
is performed. Both workflow changes must be merged into main before the first run.

Verify package visibility before anonymous host/recovery pulls. If the package
is private, make this image-only package public in GitHub package settings or
configure registry authentication on deployment and recovery hosts. Runtime
secrets are not included; do not put a registry password in the image.

Pin the deployment reference by digest. Docker retrieves a tag+digest reference
by digest, while a version tag alone can resolve to a later build.

Renovate groups official Caddy base-image and Caddy core updates. Module updates
are independent PRs. Base-image digest changes trigger rebuilds even when their
version tag is unchanged. A weekly workflow also validates and rebuilds the
currently pinned sources; it does not discover unmerged dependency updates.
The version tag may advance to a new digest; deployed digest references remain
unchanged until the deployment PR is merged and Ansible is run. Keep old build tags
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

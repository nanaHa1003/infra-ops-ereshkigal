FROM --platform=$BUILDPLATFORM caddy:2.11.9-builder-alpine@sha256:86e132d1d6db8850352b2dbc15f82a4cb25363ac305ee41e11405873e5931f37 AS builder

# renovate: datasource=github-releases depName=caddyserver/caddy
ARG CADDY_VERSION=v2.11.9
# renovate: datasource=github-tags depName=caddy-dns/cloudflare
ARG CLOUDFLARE_VERSION=v0.2.4
# renovate: datasource=github-tags depName=caddy-dns/desec
ARG DESEC_VERSION=v1.1.0
ARG TARGETOS
ARG TARGETARCH
RUN GOOS="$TARGETOS" GOARCH="$TARGETARCH" CGO_ENABLED=0 xcaddy build "$CADDY_VERSION" \
    --with "github.com/caddy-dns/cloudflare@$CLOUDFLARE_VERSION" \
    --with "github.com/caddy-dns/desec@$DESEC_VERSION" \
    --output /usr/bin/caddy

FROM caddy:2.11.9-alpine@sha256:003a97808e781aa9f7e75cc2f43d34d461099031faf5e20faefb9f61126836f0
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
LABEL org.opencontainers.image.source="https://github.com/nanaHa1003/infra-ops-ereshkigal" \
      org.opencontainers.image.description="Caddy with pinned Cloudflare and deSEC DNS providers"

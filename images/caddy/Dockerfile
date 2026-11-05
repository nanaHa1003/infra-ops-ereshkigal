FROM --platform=$BUILDPLATFORM caddy:2.11.8-builder-alpine AS builder

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

FROM caddy:2.11.8-alpine
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
LABEL org.opencontainers.image.source="https://github.com/nanaHa1003/infra-ops-ereshkigal" \
      org.opencontainers.image.description="Caddy with pinned Cloudflare and deSEC DNS providers"

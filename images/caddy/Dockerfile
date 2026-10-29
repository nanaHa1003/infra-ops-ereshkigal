FROM --platform=$BUILDPLATFORM caddy:2.11.7-builder-alpine@sha256:ef9b65adf9a224770c6d5f6d1ae1bf99bc3caaa78d8c4bae3ea949ee6984f301 AS builder

# renovate: datasource=github-releases depName=caddyserver/caddy
ARG CADDY_VERSION=v2.11.7
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

FROM caddy:2.11.7-alpine@sha256:eb4d35b7452d1ab6171fbdbe8d9275b2301dad6dad92115d91e19fb9b24d510f
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
LABEL org.opencontainers.image.source="https://github.com/nanaHa1003/infra-ops-ereshkigal" \
      org.opencontainers.image.description="Caddy with pinned Cloudflare and deSEC DNS providers"

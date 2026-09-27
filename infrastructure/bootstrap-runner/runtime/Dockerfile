FROM debian:13-slim

ARG OPENTOFU_VERSION=1.12.6

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ansible-core \
        ca-certificates \
        curl \
        git \
        jq \
        openssh-client \
        python3 \
        python3-jsonschema \
        python3-requests \
        python3-yaml \
        unzip \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    archive="tofu_${OPENTOFU_VERSION}_linux_amd64.zip"; \
    base="https://github.com/opentofu/opentofu/releases/download/v${OPENTOFU_VERSION}"; \
    curl -fsSLo "/tmp/${archive}" "${base}/${archive}"; \
    curl -fsSLo /tmp/tofu_SHA256SUMS "${base}/tofu_${OPENTOFU_VERSION}_SHA256SUMS"; \
    grep "  ${archive}$" /tmp/tofu_SHA256SUMS > /tmp/tofu.check; \
    (cd /tmp && sha256sum -c tofu.check); \
    unzip -q "/tmp/${archive}" -d /usr/local/bin; \
    rm -f "/tmp/${archive}" /tmp/tofu_SHA256SUMS /tmp/tofu.check; \
    tofu version

WORKDIR /workspace

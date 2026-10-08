ARG PACKER_IMAGE=hashicorp/packer:1.15.4@sha256:8be72877859e00e13124b26373f0505b2a2597c5393ecbea900be28b4be58186
ARG SEMAPHORE_VERSION=v2.18.30

FROM ${PACKER_IMAGE} AS packer_source
FROM semaphoreui/semaphore:${SEMAPHORE_VERSION}

USER root

ARG OPENTOFU_VERSION=1.12.6

RUN apk add --no-cache ca-certificates curl unzip git openssh-client openssl bash python3 py3-pip

RUN set -eux; \
    arch="$(apk --print-arch)"; \
    case "$arch" in \
      x86_64) tofu_arch=amd64 ;; \
      aarch64) tofu_arch=arm64 ;; \
      *) echo "Unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    tofu_zip="tofu_${OPENTOFU_VERSION}_linux_${tofu_arch}.zip"; \
    curl -fsSLo "/tmp/${tofu_zip}" \
      "https://github.com/opentofu/opentofu/releases/download/v${OPENTOFU_VERSION}/${tofu_zip}"; \
    curl -fsSLo /tmp/tofu_SHA256SUMS \
      "https://github.com/opentofu/opentofu/releases/download/v${OPENTOFU_VERSION}/tofu_${OPENTOFU_VERSION}_SHA256SUMS"; \
    grep "  ${tofu_zip}$" /tmp/tofu_SHA256SUMS > /tmp/tofu.check; \
    (cd /tmp && sha256sum -c tofu.check); \
    unzip -oq "/tmp/${tofu_zip}" -d /usr/local/bin; \
    chmod 0755 /usr/local/bin/tofu; \
    rm -f "/tmp/${tofu_zip}" /tmp/tofu_SHA256SUMS /tmp/tofu.check

COPY --from=packer_source /bin/packer /usr/local/bin/packer
RUN packer version

COPY requirements.txt /etc/semaphore/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /etc/semaphore/requirements.txt

COPY ssh_config /etc/ssh/ssh_config.d/99-infra-manager.conf
RUN rm -f /etc/ssh/ssh_config.d/semaphore.conf

USER 1001:0

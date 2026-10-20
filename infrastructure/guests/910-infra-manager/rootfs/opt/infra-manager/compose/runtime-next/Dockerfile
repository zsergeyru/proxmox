# Следующее поколение infra-runtime без Semaphore.
# До переключения штатной активации образ проверяется отдельно от текущего.
ARG PACKER_IMAGE=hashicorp/packer:1.15.4@sha256:8be72877859e00e13124b26373f0505b2a2597c5393ecbea900be28b4be58186
FROM ${PACKER_IMAGE} AS packer_source

FROM python:3.12.12-slim-bookworm
LABEL org.proxmox.runtime.generation="standalone-v1"

ARG OPENTOFU_VERSION=1.12.6
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates curl unzip git openssh-client openssl bash \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64|arm64) : ;; \
      *) echo "Unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    archive="tofu_${OPENTOFU_VERSION}_linux_${arch}.zip"; \
    curl -fsSLo "/tmp/$archive" \
      "https://github.com/opentofu/opentofu/releases/download/v${OPENTOFU_VERSION}/${archive}"; \
    curl -fsSLo /tmp/SHA256SUMS \
      "https://github.com/opentofu/opentofu/releases/download/v${OPENTOFU_VERSION}/tofu_${OPENTOFU_VERSION}_SHA256SUMS"; \
    grep "  ${archive}$" /tmp/SHA256SUMS > /tmp/tofu.check; \
    (cd /tmp && sha256sum -c tofu.check); \
    unzip -oq "/tmp/$archive" -d /usr/local/bin; \
    chmod 0755 /usr/local/bin/tofu; \
    rm -f "/tmp/$archive" /tmp/SHA256SUMS /tmp/tofu.check

COPY --from=packer_source /bin/packer /usr/local/bin/packer
COPY runtime/requirements.txt /tmp/infra-runtime-requirements.txt
RUN python3 -m pip install --no-cache-dir \
       ansible-core==2.19.4 \
       -r /tmp/infra-runtime-requirements.txt \
    && rm -f /tmp/infra-runtime-requirements.txt \
    && useradd --uid 1001 --gid 0 --no-create-home \
       --shell /usr/sbin/nologin infra-runtime \
    && install -d -o 1001 -g 0 -m 0700 /home/infra-runtime

# Git SHA фиксируется в последнем лёгком слое: смена коммита не должна
# запускать заново сборку Python, OpenTofu и Packer.
ARG INFRA_GIT_REVISION=unknown
ARG INFRA_RUNTIME_CONTENT_REVISION=unknown
ENV INFRA_RUNTIME_BUILD_GIT_REVISION=${INFRA_GIT_REVISION}
ENV INFRA_RUNTIME_BUILD_REVISION=${INFRA_RUNTIME_CONTENT_REVISION}

USER 1001:0
WORKDIR /var/lib/infra-manager/bootstrap-repo
CMD ["sleep", "infinity"]

#!/usr/bin/env bash
set -Eeuo pipefail

[[ $EUID -eq 0 ]] || { echo "ОШИБКА: сценарий должен выполняться от root внутри LXC 990" >&2; exit 1; }

REPO_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
CONFIG_DIR="/etc/bootstrap-runner"
DATA_DIR="/var/lib/bootstrap-runner"
IMAGE="bootstrap-runtime:v1"

command -v apt-get >/dev/null 2>&1 || { echo "ОШИБКА: не найден apt-get" >&2; exit 1; }

install -d -m 0700 "$CONFIG_DIR/secrets" "$CONFIG_DIR/bootstrap-ssh"
install -d -m 0755 "$CONFIG_DIR/ca" "$DATA_DIR/opentofu/state"

if ! command -v docker >/dev/null 2>&1; then
    apt-get update
    # В Debian 13 docker.io содержит демон, а клиентская команда docker
    # поставляется отдельным пакетом docker-cli. При --no-install-recommends
    # его нужно устанавливать явно.
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        ca-certificates docker.io docker-cli openssh-client
fi

command -v docker >/dev/null 2>&1 || {
    echo "ОШИБКА: после установки не найдена команда docker" >&2
    exit 1
}

systemctl enable --now docker >/dev/null

rm -f \
    "$CONFIG_DIR/bootstrap-ssh/infra_manager_ed25519" \
    "$CONFIG_DIR/bootstrap-ssh/infra_manager_ed25519.pub"
ssh-keygen -q -t ed25519 -N '' \
    -C infra-manager-bootstrap \
    -f "$CONFIG_DIR/bootstrap-ssh/infra_manager_ed25519"
chmod 0600 "$CONFIG_DIR/bootstrap-ssh/infra_manager_ed25519"
chmod 0644 "$CONFIG_DIR/bootstrap-ssh/infra_manager_ed25519.pub"

docker build     --build-arg OPENTOFU_VERSION=1.12.6     -t "$IMAGE"     "$REPO_ROOT/infrastructure/bootstrap-runner/runtime"

docker run --rm "$IMAGE" tofu version >/dev/null
docker run --rm "$IMAGE" ansible --version >/dev/null
docker run --rm "$IMAGE" python3 -c 'import yaml, jsonschema, requests' >/dev/null

printf '[ОК] Среда bootstrap-runner 990 подготовлена\n'

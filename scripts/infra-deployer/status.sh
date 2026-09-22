#!/usr/bin/env bash
set -Eeuo pipefail

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ОК] %s\n' "$*"; }

required_file() {
    [[ -s "$1" ]] || die "Отсутствует обязательный файл: $1"
}

command -v docker >/dev/null 2>&1 || die "Docker не установлен"
docker compose version >/dev/null 2>&1 || die "Docker Compose недоступен"
[[ -x /usr/local/sbin/infra-deployer-pve-access-check ]]     || die "Отсутствует infra-deployer-pve-access-check"

required_file /etc/infra-deployer/secrets/pve-api.env
required_file /etc/infra-deployer/secrets/semaphore-server.env
required_file /etc/infra-deployer/secrets/semaphore-runner.env
required_file /etc/infra-deployer/secrets/semaphore-api-token
required_file /etc/infra-deployer/secrets/github_project_ed25519
required_file /etc/infra-deployer/secrets/ansible_ed25519
required_file /var/lib/infra-deployer/public-keys/ansible_ed25519.pub
required_file /var/lib/infra-deployer/semaphore/project-id
required_file /etc/infra-deployer/ca/ca-bundle.crt

[[ -d /var/lib/infra-deployer/opentofu/state ]]     || die "Отсутствует каталог OpenTofu state"

[[ "$(docker inspect -f '{{.State.Running}}' infra-deployer-semaphore 2>/dev/null || true)" == "true" ]]     || die "Semaphore Server не запущен"
[[ "$(docker inspect -f '{{.State.Running}}' infra-deployer-runner 2>/dev/null || true)" == "true" ]]     || die "Semaphore Runner не запущен"

curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:3000/api/ping >/dev/null     || die "Semaphore API не отвечает"

docker exec infra-deployer-runner tofu version >/dev/null     || die "OpenTofu недоступен"
docker exec infra-deployer-runner packer version >/dev/null     || die "Packer недоступен"
docker exec infra-deployer-runner ansible --version >/dev/null     || die "Ansible недоступен"
docker exec infra-deployer-runner python3 -c 'import proxmoxer' >/dev/null     || die "proxmoxer недоступен"

/usr/local/sbin/infra-deployer-pve-access-check >/dev/null     || die "PVE API access не соответствует контракту"

ok "infra-deployer готов"

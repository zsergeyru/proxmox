#!/usr/bin/env bash
set -Eeuo pipefail

# Первоначальная и повторяемая настройка 910 infra-deployer.
# Скрипт запускается внутри LXC 910 от root публичным bootstrap.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ASSET_DIR="${REPO_ROOT}/guests/910-infra-deployer/compose"

CONFIG_DIR="/etc/infra-deployer"
SECRET_DIR="${CONFIG_DIR}/secrets"
CA_DIR="${CONFIG_DIR}/ca"
DATA_DIR="/var/lib/infra-deployer"
SEMAPHORE_DIR="${DATA_DIR}/semaphore"
RUNNER_DIR="${DATA_DIR}/runner"
RUNNER_TMP_DIR="${RUNNER_DIR}/tmp"
OPENTOFU_DIR="${DATA_DIR}/opentofu"
STATE_DIR="${OPENTOFU_DIR}/state"
OPENTOFU_INPUT="${OPENTOFU_DIR}/guests.json"
PUBLIC_KEY_DIR="${DATA_DIR}/public-keys"
COMPOSE_DIR="/opt/infra-deployer/compose"
STATUS_COMMAND="/usr/local/sbin/infra-deployer-status"
ACCESS_CHECK_COMMAND="/usr/local/sbin/infra-deployer-pve-access-check"
LIFECYCLE_TEST_COMMAND="/usr/local/sbin/infra-deployer-pve-lifecycle-test"

SERVER_ENV="${SECRET_DIR}/semaphore-server.env"
RUNNER_ENV="${SECRET_DIR}/semaphore-runner.env"
PVE_API_ENV="${SECRET_DIR}/pve-api.env"
ADMIN_PASSWORD_FILE="${SECRET_DIR}/initial-admin-password"
CA_BUNDLE="${CA_DIR}/ca-bundle.crt"

PVE_API_SECRET_FILE="${PVE_API_SECRET_FILE:-}"
RECOVER="${INFRA_DEPLOYER_RECOVER:-0}"

SEMAPHORE_VERSION="v2.18.30"
PROJECT_BRANCH="${INFRA_PROJECT_BRANCH:-infra-iac-redesign}"
OPENTOFU_VERSION="1.12.6"
PACKER_VERSION="1.15.4"

log()  { printf '\n==> %s\n' "$*"; }
ok()   { printf '[ОК] %s\n' "$*"; }
die()  { printf '\nОШИБКА: %s\n' "$*" >&2; exit 1; }

require_root() {
    [[ $EUID -eq 0 ]] || die "setup.sh должен выполняться от root"
}

check_os() {
    # shellcheck source=/dev/null
    . /etc/os-release
    [[ "${ID:-}" == "debian" ]] || die "Ожидается Debian"
    [[ "${VERSION_ID:-}" == "13" ]] || die "Ожидается Debian 13, обнаружено ${VERSION_ID:-?}"
    [[ "$(dpkg --print-architecture)" == "amd64" ]]         || die "Первая версия infra-deployer рассчитана на amd64"
}

prepare_directories() {
    install -d -o root -g root -m 0755 "$CONFIG_DIR" "$CA_DIR" "$DATA_DIR" "$COMPOSE_DIR"
    install -d -o root -g root -m 0700 "$SECRET_DIR"
    install -d -o 1001 -g 0 -m 0750 "$SEMAPHORE_DIR" "$RUNNER_DIR" "$RUNNER_TMP_DIR" "$OPENTOFU_DIR" "$STATE_DIR"
    install -d -o root -g root -m 0755 "$PUBLIC_KEY_DIR"
}

ensure_base_packages() {
    local missing="" pkg

    for pkg in ca-certificates curl gnupg python3-yaml; do
        dpkg -s "$pkg" >/dev/null 2>&1 || missing="$missing $pkg"
    done

    if [[ -z "$missing" ]]; then
        ok "Базовые пакеты infra-deployer уже установлены"
        return
    fi

    log "Установка базовых пакетов infra-deployer"
    apt-get update
    # shellcheck disable=SC2086
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $missing
    ok "Базовые пакеты infra-deployer установлены"
}

install_docker() {
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        systemctl enable --now docker >/dev/null
        ok "Docker Engine и Compose уже доступны"
        return
    fi

    local pkg
    for pkg in docker.io docker-compose docker-doc podman-docker containerd runc; do
        if dpkg -s "$pkg" >/dev/null 2>&1; then
            die "Обнаружен конфликтующий пакет $pkg. Удалите его осознанно перед установкой Docker CE."
        fi
    done

    log "Установка Docker Engine из официального репозитория"
    apt-get update

    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/debian/gpg         -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc

    local arch codename
    arch="$(dpkg --print-architecture)"
    # shellcheck source=/dev/null
    . /etc/os-release
    codename="${VERSION_CODENAME:-trixie}"

    cat >/etc/apt/sources.list.d/docker.sources <<EOF_DOCKER
Types: deb
URIs: https://download.docker.com/linux/debian
Suites: ${codename}
Components: stable
Architectures: ${arch}
Signed-By: /etc/apt/keyrings/docker.asc
EOF_DOCKER

    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends         docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    systemctl enable --now docker
    docker version >/dev/null
    docker compose version >/dev/null
    ok "Docker Engine и Compose установлены"
}

copy_compose_assets() {
    [[ -f "$ASSET_DIR/docker-compose.yml" ]] || die "Не найден docker-compose.yml в проекте"
    [[ -f "$ASSET_DIR/runner/Dockerfile" ]] || die "Не найден Dockerfile Runner"
    [[ -f "$ASSET_DIR/runner/requirements.txt" ]] || die "Не найден requirements.txt Runner"
    [[ -f "$ASSET_DIR/runner/ssh_config" ]] || die "Не найден строгий SSH config Runner"

    install -m 0644 "$ASSET_DIR/docker-compose.yml" "$COMPOSE_DIR/docker-compose.yml"
    install -d -m 0755 "$COMPOSE_DIR/runner"
    install -m 0644 "$ASSET_DIR/runner/Dockerfile" "$COMPOSE_DIR/runner/Dockerfile"
    install -m 0644 "$ASSET_DIR/runner/requirements.txt" "$COMPOSE_DIR/runner/requirements.txt"
    install -m 0644 "$ASSET_DIR/runner/ssh_config" "$COMPOSE_DIR/runner/ssh_config"
}

seed_runner_known_hosts() {
    local source="/root/.ssh/github_known_hosts"
    local target="$RUNNER_DIR/known_hosts"

    if [[ -s "$source" ]]; then
        install -o 1001 -g 0 -m 0644 "$source" "$target"
    elif [[ ! -s "$target" ]]; then
        curl -fsSL --connect-timeout 10 --max-time 20 https://api.github.com/meta \
            | jq -r '.ssh_keys[] | "github.com " + .' >"$target"
        chown 1001:0 "$target"
        chmod 0644 "$target"
    fi

    [[ -s "$target" ]] || die "Не удалось подготовить known_hosts Runner"
}

generate_ca_bundle() {
    local pve_ca="/usr/local/share/ca-certificates/pve-root-ca.crt"
    [[ -s /etc/ssl/certs/ca-certificates.crt ]] || die "Не найден системный CA bundle"
    [[ -s "$pve_ca" ]] || die "Не найден PVE CA, который должен передать public bootstrap"

    cat /etc/ssl/certs/ca-certificates.crt "$pve_ca" >"$CA_BUNDLE"
    chmod 0644 "$CA_BUNDLE"
}

persist_pve_api_secret() {
    [[ -n "$PVE_API_SECRET_FILE" ]] || die "Не задан PVE_API_SECRET_FILE"
    [[ -s "$PVE_API_SECRET_FILE" ]] || die "PVE API staging secret отсутствует: $PVE_API_SECRET_FILE"

    if [[ -f "$PVE_API_ENV" && "$RECOVER" != "1" ]]; then
        cmp -s "$PVE_API_SECRET_FILE" "$PVE_API_ENV"             || die "Постоянный PVE API secret уже существует и отличается. Для замены требуется recovery."
        ok "Постоянный PVE API secret уже совпадает со staging secret"
        return
    fi

    install -o root -g root -m 0600 "$PVE_API_SECRET_FILE" "$PVE_API_ENV"
    ok "PVE API credential сохранён в защищённом постоянном хранилище"
}

random_base64() {
    openssl rand -base64 "$1" | tr -d '\n'
}

ensure_semaphore_secrets() {
    command -v openssl >/dev/null 2>&1 || {
        apt-get update
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssl
    }

    if [[ ! -f "$SERVER_ENV" ]]; then
        local admin_password encryption_key runner_registration_token timezone
        admin_password="$(random_base64 24)"
        encryption_key="$(random_base64 32)"
        runner_registration_token="$(openssl rand -hex 32)"
        timezone="$(timedatectl show --property=Timezone --value 2>/dev/null || true)"
        [[ -n "$timezone" ]] || timezone="UTC"

        umask 077
        cat >"$SERVER_ENV" <<EOF_SERVER
SEMAPHORE_DB_DIALECT=sqlite
SEMAPHORE_DB_HOST=/var/lib/semaphore/semaphore.sqlite
SEMAPHORE_ADMIN=admin
SEMAPHORE_ADMIN_PASSWORD=${admin_password}
SEMAPHORE_ADMIN_NAME=Admin
SEMAPHORE_ADMIN_EMAIL=admin@localhost
SEMAPHORE_ACCESS_KEY_ENCRYPTION=${encryption_key}
SEMAPHORE_USE_REMOTE_RUNNER=True
SEMAPHORE_RUNNER_REGISTRATION_TOKEN=${runner_registration_token}
TZ=${timezone}
EOF_SERVER

        cat >"$RUNNER_ENV" <<EOF_RUNNER
SEMAPHORE_WEB_ROOT=http://semaphore:3000
SEMAPHORE_RUNNER_REGISTRATION_TOKEN=${runner_registration_token}
SEMAPHORE_RUNNER_NAME=infra-deployer
SEMAPHORE_RUNNER_PRIVATE_KEY_FILE=/var/lib/semaphore/runner.key
SEMAPHORE_RUNNER_MAX_PARALLEL_TASKS=1
ANSIBLE_HOST_KEY_CHECKING=True
SSL_CERT_FILE=/etc/infra-deployer/ca/ca-bundle.crt
REQUESTS_CA_BUNDLE=/etc/infra-deployer/ca/ca-bundle.crt
GIT_SSL_CAINFO=/etc/infra-deployer/ca/ca-bundle.crt
EOF_RUNNER

        printf '%s\n' "$admin_password" >"$ADMIN_PASSWORD_FILE"
        chmod 0600 "$SERVER_ENV" "$RUNNER_ENV" "$ADMIN_PASSWORD_FILE"
        ok "Созданы первичные секреты Semaphore"
    else
        [[ -s "$RUNNER_ENV" ]] || die "Есть server env, но отсутствует runner env"
        [[ -s "$ADMIN_PASSWORD_FILE" ]] || die "Есть server env, но отсутствует initial admin password"
        ok "Используются существующие секреты Semaphore"
    fi
}

prepare_opentofu_input() {
    log "Подготовка итогового состояния гостей для OpenTofu"

    python3 "$REPO_ROOT/scripts/infra-deployer/render-opentofu-input.py" \
        --output "$OPENTOFU_INPUT"
    chown 1001:0 "$OPENTOFU_INPUT"
    chmod 0640 "$OPENTOFU_INPUT"

    ok "OpenTofu input подготовлен: $OPENTOFU_INPUT"
}

write_runtime_versions() {
    cat >"$COMPOSE_DIR/.versions.env" <<EOF_VERSIONS
SEMAPHORE_VERSION=${SEMAPHORE_VERSION}
OPENTOFU_VERSION=${OPENTOFU_VERSION}
PACKER_VERSION=${PACKER_VERSION}
EOF_VERSIONS
    chmod 0644 "$COMPOSE_DIR/.versions.env"
}

compose() {
    docker compose         --env-file "$COMPOSE_DIR/.versions.env"         -f "$COMPOSE_DIR/docker-compose.yml" "$@"
}

deploy_semaphore() {
    log "Сборка и запуск Semaphore"
    compose pull semaphore
    compose build --pull runner
    compose up -d --remove-orphans
}

wait_semaphore() {
    local _
    log "Проверка Semaphore"

    for _ in $(seq 1 60); do
        if curl -fsS --connect-timeout 2 --max-time 5             http://127.0.0.1:3000/ >/dev/null 2>&1; then
            break
        fi
        sleep 2
    done

    curl -fsS --connect-timeout 2 --max-time 5 http://127.0.0.1:3000/ >/dev/null         || {
            compose ps
            compose logs --tail=100 semaphore
            die "Semaphore Server не стал доступен"
        }

    sleep 5

    [[ "$(docker inspect -f '{{.State.Running}}' infra-deployer-semaphore 2>/dev/null || true)" == "true" ]]         || die "Semaphore Server container не запущен"
    [[ "$(docker inspect -f '{{.State.Running}}' infra-deployer-runner 2>/dev/null || true)" == "true" ]]         || {
            compose logs --tail=100 runner
            die "Semaphore Runner container не запущен"
        }

    ok "Semaphore Server и Runner запущены"
}

configure_semaphore_project() {
    log "Настройка проекта Semaphore"
    INFRA_PROJECT_BRANCH="$PROJECT_BRANCH" \
    INFRA_DEPLOYER_RECOVER="$RECOVER" \
        bash "$REPO_ROOT/scripts/infra-deployer/semaphore-project.sh"
}

install_local_commands() {
    install -o root -g root -m 0755 \
        "$REPO_ROOT/scripts/infra-deployer/status.sh" "$STATUS_COMMAND"
    install -o root -g root -m 0755 \
        "$REPO_ROOT/scripts/infra-deployer/check-pve-access.sh" "$ACCESS_CHECK_COMMAND"
    install -o root -g root -m 0755 \
        "$REPO_ROOT/scripts/infra-deployer/test-pve-lifecycle.sh" "$LIFECYCLE_TEST_COMMAND"
}

verify_runner_tools() {
    log "Проверка инструментов Runner"

    docker exec infra-deployer-runner tofu version >/dev/null         || die "OpenTofu отсутствует в Runner"
    docker exec infra-deployer-runner packer version >/dev/null         || die "Packer отсутствует в Runner"
    docker exec infra-deployer-runner ansible --version >/dev/null         || die "Ansible отсутствует в Runner"
    docker exec infra-deployer-runner python3 -c 'import proxmoxer'         || die "Python-модуль proxmoxer отсутствует в Runner"

    ok "OpenTofu, Packer, Ansible и proxmoxer доступны"
}

report_result() {
    local address
    address="$(hostname -I 2>/dev/null | awk '{print $1}')"

    printf '\n910 infra-deployer подготовлен.\n'
    if [[ -n "$address" ]]; then
        printf 'Semaphore: http://%s:3000/\n' "$address"
    else
        printf 'Semaphore: порт 3000 LXC 910\n'
    fi
    printf 'Пользователь: admin\n'
    printf 'Первичный пароль хранится: %s\n' "$ADMIN_PASSWORD_FILE"
    printf 'PVE API credential хранится: %s\n' "$PVE_API_ENV"
}

main() {
    require_root
    check_os
    prepare_directories
    ensure_base_packages
    install_docker
    copy_compose_assets
    seed_runner_known_hosts
    generate_ca_bundle
    persist_pve_api_secret
    ensure_semaphore_secrets
    prepare_opentofu_input
    write_runtime_versions
    deploy_semaphore
    wait_semaphore
    verify_runner_tools
    configure_semaphore_project
    install_local_commands
    "$STATUS_COMMAND"
    report_result
}

main "$@"

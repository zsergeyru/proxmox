#!/usr/bin/env bash
set -Eeuo pipefail

# Минимальный Debian в LXC может наследовать locale хоста, которого нет внутри 910.
# C.UTF-8 доступен штатно и не требует установки пакета locales.
export LANG=C.UTF-8
export LC_ALL=C.UTF-8

# Первоначальная и повторяемая настройка 910 infra-deployer.
# Скрипт запускается внутри LXC 910 от root единым bootstrap через pct exec.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ASSET_DIR="${REPO_ROOT}/guests/910-infra-deployer/compose"

CONFIG_DIR="/etc/infra-deployer"
SECRET_DIR="${CONFIG_DIR}/secrets"
CA_DIR="${CONFIG_DIR}/ca"
DATA_DIR="/var/lib/infra-deployer"
SEMAPHORE_DIR="${DATA_DIR}/semaphore"
OPENTOFU_DIR="${DATA_DIR}/opentofu"
STATE_DIR="${OPENTOFU_DIR}/state"
OPENTOFU_INPUT="${OPENTOFU_DIR}/guests.json"
COMPOSE_DIR="/opt/infra-deployer/compose"
STATUS_COMMAND="/usr/local/sbin/infra-deployer-status"
ACCESS_CHECK_COMMAND="/usr/local/sbin/infra-deployer-pve-access-check"
LIFECYCLE_TEST_COMMAND="/usr/local/sbin/infra-deployer-pve-lifecycle-test"

SERVER_ENV="${SECRET_DIR}/semaphore-server.env"
PVE_API_ENV="${SECRET_DIR}/pve-api.env"
ADMIN_PASSWORD_FILE="${SECRET_DIR}/initial-admin-password"
ADMIN_PASSWORD_SHOWN_FILE="${SECRET_DIR}/.initial-admin-password-shown"
CA_BUNDLE="${CA_DIR}/ca-bundle.crt"

PVE_API_SECRET_FILE="${PVE_API_SECRET_FILE:-}"
RECOVER="${INFRA_DEPLOYER_RECOVER:-0}"

SEMAPHORE_VERSION="v2.18.30"
PROJECT_BRANCH="${INFRA_PROJECT_BRANCH:-infra-iac-redesign}"
OPENTOFU_VERSION="1.12.6"
PACKER_VERSION="1.15.4"
LOG_FILE="${INFRA_DEPLOYER_LOG_FILE:-/var/log/infra-deployer/bootstrap.log}"

C_RESET=""
C_BOLD=""
C_GREEN=""
C_BLUE=""
C_RED=""

if [[ "${INFRA_DEPLOYER_COLOR:-0}" == "1" && "${NO_COLOR:-}" == "" ]]; then
    C_RESET="$(printf '\033[0m')"
    C_BOLD="$(printf '\033[1m')"
    C_GREEN="$(printf '\033[32m')"
    C_BLUE="$(printf '\033[34m')"
    C_RED="$(printf '\033[31m')"
fi

write_log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$LOG_FILE"
}

log() {
    write_log "ЭТАП: $*"
    printf '\n%s%s==> %s%s\n' "$C_BOLD" "$C_BLUE" "$*" "$C_RESET"
}

ok() {
    write_log "ОК: $*"
    printf '%s%s[ОК]%s %s\n' "$C_BOLD" "$C_GREEN" "$C_RESET" "$*"
}

die() {
    write_log "ОШИБКА: $*"
    printf '\n%s%sОШИБКА:%s %s\n' "$C_BOLD" "$C_RED" "$C_RESET" "$*" >&2
    printf 'Полный технический лог: %s\n' "$LOG_FILE" >&2
    exit 1
}

init_log() {
    install -d -o root -g root -m 0755 "$(dirname "$LOG_FILE")"
    touch "$LOG_FILE"
    chmod 0640 "$LOG_FILE"
    printf '\n===== setup infra-deployer %s =====\n' "$(date '+%Y-%m-%d %H:%M:%S')" >>"$LOG_FILE"
}

run_logged() {
    local rc=0
    write_log "КОМАНДА: $*"
    "$@" >>"$LOG_FILE" 2>&1 || rc=$?
    if ((rc != 0)); then
        printf '%s%sОШИБКА:%s команда завершилась с кодом %s\n' "$C_BOLD" "$C_RED" "$C_RESET" "$rc" >&2
        printf 'Последние строки лога:\n' >&2
        tail -n 25 "$LOG_FILE" >&2 || true
        printf 'Полный технический лог: %s\n' "$LOG_FILE" >&2
        return "$rc"
    fi
}

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
    install -d -o 1001 -g 0 -m 0770 "$SEMAPHORE_DIR"
    install -d -o 1001 -g 0 -m 0750 "$OPENTOFU_DIR" "$STATE_DIR"

    # Semaphore Server в официальном образе работает от UID 1001 и группы 0.
    # Для уже существующего каталога install -d недостаточно: явно восстанавливаем
    # владельца и право записи группы, нужное SQLite для journal/WAL-файлов.
    chown -R 1001:0 "$SEMAPHORE_DIR"
    chmod 0770 "$SEMAPHORE_DIR"
}

ensure_base_packages() {
    local missing="" pkg

    for pkg in ca-certificates curl git gnupg jq openssh-client openssl python3-yaml; do
        dpkg -s "$pkg" >/dev/null 2>&1 || missing="$missing $pkg"
    done

    if [[ -z "$missing" ]]; then
        ok "Базовые пакеты infra-deployer уже установлены"
        return
    fi

    log "Установка базовых пакетов infra-deployer"
    run_logged apt-get update
    # shellcheck disable=SC2086
    run_logged env DEBIAN_FRONTEND=noninteractive \
        apt-get install -y --no-install-recommends $missing
    ok "Базовые пакеты infra-deployer установлены"
}

install_docker() {
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        run_logged systemctl enable --now docker
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
    run_logged apt-get update

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

    run_logged apt-get update
    run_logged env DEBIAN_FRONTEND=noninteractive \
        apt-get install -y --no-install-recommends \
        docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

    run_logged systemctl enable --now docker
    docker version >/dev/null
    docker compose version >/dev/null
    ok "Docker Engine и Compose установлены"
}

copy_compose_assets() {
    [[ -f "$ASSET_DIR/docker-compose.yml" ]] || die "Не найден docker-compose.yml в проекте"
    [[ -f "$ASSET_DIR/semaphore/Dockerfile" ]] || die "Не найден Dockerfile Semaphore"
    [[ -f "$ASSET_DIR/semaphore/requirements.txt" ]] || die "Не найден requirements.txt Semaphore"
    [[ -f "$ASSET_DIR/semaphore/ssh_config" ]] || die "Не найден строгий SSH config Semaphore"

    install -m 0644 "$ASSET_DIR/docker-compose.yml" "$COMPOSE_DIR/docker-compose.yml"
    install -d -m 0755 "$COMPOSE_DIR/semaphore"
    install -m 0644 "$ASSET_DIR/semaphore/Dockerfile" "$COMPOSE_DIR/semaphore/Dockerfile"
    install -m 0644 "$ASSET_DIR/semaphore/requirements.txt" "$COMPOSE_DIR/semaphore/requirements.txt"
    install -m 0644 "$ASSET_DIR/semaphore/ssh_config" "$COMPOSE_DIR/semaphore/ssh_config"
}

seed_semaphore_known_hosts() {
    local source="/root/.ssh/github_known_hosts"
    local target="$SEMAPHORE_DIR/known_hosts"

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
    if [[ -f "$PVE_API_ENV" ]]; then
        if [[ -n "$PVE_API_SECRET_FILE" && -s "$PVE_API_SECRET_FILE" ]]; then
            if cmp -s "$PVE_API_SECRET_FILE" "$PVE_API_ENV"; then
                ok "Постоянный PVE API credential уже актуален"
                return
            fi

            [[ "$RECOVER" == "1" ]] \
                || die "Постоянный PVE API credential отличается от переданного. Для замены требуется recovery."

            install -o root -g root -m 0600 "$PVE_API_SECRET_FILE" "$PVE_API_ENV"
            ok "PVE API credential заменён в режиме recovery"
            return
        fi

        ok "Используется существующий постоянный PVE API credential"
        return
    fi

    [[ -n "$PVE_API_SECRET_FILE" ]] || die "Не задан PVE_API_SECRET_FILE"
    [[ -s "$PVE_API_SECRET_FILE" ]] || die "PVE API staging secret отсутствует: $PVE_API_SECRET_FILE"

    install -o root -g root -m 0600 "$PVE_API_SECRET_FILE" "$PVE_API_ENV"
    ok "PVE API credential сохранён в защищённом постоянном хранилище"
}

random_base64() {
    openssl rand -base64 "$1" | tr -d '\n'
}

ensure_semaphore_secrets() {
    if [[ ! -f "$SERVER_ENV" ]]; then
        local admin_password encryption_key timezone
        admin_password="$(random_base64 24)"
        encryption_key="$(random_base64 32)"
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
TZ=${timezone}
EOF_SERVER

        printf '%s\n' "$admin_password" >"$ADMIN_PASSWORD_FILE"
        chmod 0600 "$SERVER_ENV" "$ADMIN_PASSWORD_FILE"
        ok "Созданы первичные секреты Semaphore"
    else
        [[ -s "$ADMIN_PASSWORD_FILE" ]] || die "Есть server env, но отсутствует initial admin password"

        # Старый вариант использовал отдельный remote Runner. Для одного домашнего
        # 910 он не нужен: сам Semaphore выполняет задания локально.
        sed -i \
            -e '/^SEMAPHORE_USE_REMOTE_RUNNER=/d' \
            -e '/^SEMAPHORE_RUNNER_REGISTRATION_TOKEN=/d' \
            "$SERVER_ENV"
        rm -f "$SECRET_DIR/semaphore-runner.env"
        chmod 0600 "$SERVER_ENV"

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

repair_semaphore_storage() {
    log "Проверка прав хранилища Semaphore"

    # Права восстанавливаются через тот же Docker runtime, который запускает
    # Semaphore. Это важно для вложенного Docker в LXC: uid/gid, видимые
    # процессу контейнера, должны иметь доступ к bind mount SQLite.
    run_logged docker run --rm \
        --user 0:0 \
        --entrypoint /bin/sh \
        -v "$SEMAPHORE_DIR:/var/lib/semaphore" \
        "semaphoreui/semaphore:${SEMAPHORE_VERSION}" \
        -c '
            set -eu
            chown -R 1001:0 /var/lib/semaphore
            find /var/lib/semaphore -type d -exec chmod 0770 {} +
            find /var/lib/semaphore -type f -exec chmod 0660 {} +
        ' \
        || die "Не удалось восстановить права хранилища Semaphore через Docker"

    run_logged docker run --rm \
        --user 1001:0 \
        --entrypoint /bin/sh \
        -v "$SEMAPHORE_DIR:/var/lib/semaphore" \
        "semaphoreui/semaphore:${SEMAPHORE_VERSION}" \
        -c '
            set -eu
            probe="/var/lib/semaphore/.write-test"
            : >"$probe"
            rm -f "$probe"
        ' \
        || die "UID 1001 контейнера Semaphore не может писать в постоянное хранилище"

    ok "Хранилище Semaphore доступно для записи из контейнера"
}

deploy_semaphore() {
    log "Сборка и запуск Semaphore"

    # Базовый образ нужен и для восстановления прав хранилища, и для сборки
    # нашего единственного контейнера с OpenTofu/Packer/Ansible.
    run_logged docker pull "semaphoreui/semaphore:${SEMAPHORE_VERSION}"

    compose stop semaphore >/dev/null 2>&1 || true
    repair_semaphore_storage

    run_logged compose build --pull semaphore
    run_logged compose up -d --remove-orphans
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
            compose ps >>"$LOG_FILE" 2>&1 || true
            compose logs --tail=100 semaphore >>"$LOG_FILE" 2>&1 || true
            die "Semaphore Server не стал доступен"
        }

    sleep 5

    [[ "$(docker inspect -f '{{.State.Running}}' infra-deployer-semaphore 2>/dev/null || true)" == "true" ]]         || die "Semaphore container не запущен"

    ok "Semaphore запущен"
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

    # pct exec использует PATH без /usr/local/sbin. Канонические файлы остаются
    # в sbin, а короткие команды доступны через /usr/local/bin.
    install -d -o root -g root -m 0755 /usr/local/bin
    ln -sfn "$STATUS_COMMAND" /usr/local/bin/infra-deployer-status
    ln -sfn "$ACCESS_CHECK_COMMAND" /usr/local/bin/infra-deployer-pve-access-check
    ln -sfn "$LIFECYCLE_TEST_COMMAND" /usr/local/bin/infra-deployer-pve-lifecycle-test
}

verify_semaphore_tools() {
    log "Проверка инструментов Semaphore"

    docker exec infra-deployer-semaphore tofu version >/dev/null         || die "OpenTofu отсутствует в Semaphore"
    docker exec infra-deployer-semaphore packer version >/dev/null         || die "Packer отсутствует в Semaphore"
    docker exec infra-deployer-semaphore ansible --version >/dev/null         || die "Ansible отсутствует в Semaphore"
    docker exec infra-deployer-semaphore python3 -c 'import proxmoxer'         || die "Python-модуль proxmoxer отсутствует в Semaphore"

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
    if [[ ! -e "$ADMIN_PASSWORD_SHOWN_FILE" ]]; then
        printf 'Пароль: %s\n' "$(cat "$ADMIN_PASSWORD_FILE")"
        printf 'Пароль показан один раз и сохранён: %s\n' "$ADMIN_PASSWORD_FILE"
        : >"$ADMIN_PASSWORD_SHOWN_FILE"
        chmod 0600 "$ADMIN_PASSWORD_SHOWN_FILE"
    else
        printf 'Пароль сохранён: %s\n' "$ADMIN_PASSWORD_FILE"
    fi
    printf 'PVE API credential хранится: %s\n' "$PVE_API_ENV"
}

main() {
    require_root
    init_log
    check_os
    prepare_directories
    ensure_base_packages
    install_docker
    copy_compose_assets
    seed_semaphore_known_hosts
    generate_ca_bundle
    persist_pve_api_secret
    ensure_semaphore_secrets
    prepare_opentofu_input
    write_runtime_versions
    deploy_semaphore
    wait_semaphore
    verify_semaphore_tools
    configure_semaphore_project
    install_local_commands
    "$STATUS_COMMAND"
    report_result
}

main "$@"

#!/usr/bin/env bash
set -Eeuo pipefail

# =============================================================================
# Приватная стадия инициализации Proxmox VE
# =============================================================================
#
# Этот скрипт является канонической полной инициализацией PVE.
# Публичный zsergeyru/proxmox-bootstrap/init-pve.sh выполняет только Stage 0:
# устанавливает минимальный Git/SSH-набор, создаёт read-only Deploy Key,
# получает доступ к этому приватному репозиторию и передаёт управление сюда.
#
# Скрипт можно запускать повторно. Для проектных ролей он безопасно добавляет
# недостающие privileges, но не удаляет уже существующие. Существующие ACL и
# свойства API-токенов также не сужаются автоматически.

BOOTSTRAP_VERSION=7

PRIVATE_REPO="git@github.com:zsergeyru/proxmox.git"
PRIVATE_BRANCH="main"

# Временные файлы Stage 0. После успешного завершения этой стадии публичный
# bootstrap удалит временную область целиком. Постоянный Deploy Key хранится
# уже в /etc/proxmox-deployer/ssh, а bootstrap state — в RUNTIME_DIR/state.
STAGE0_DIR="${PVE_STAGE0_DIR:-/var/lib/proxmox-bootstrap}"
STAGE0_KEY_FILE="${PVE_STAGE0_KEY_FILE:-${STAGE0_DIR}/github_proxmox_repo_ed25519}"
STAGE0_KEY_PUB_FILE="${STAGE0_KEY_FILE}.pub"
STAGE0_KNOWN_HOSTS="${PVE_STAGE0_KNOWN_HOSTS:-${STAGE0_DIR}/known_hosts}"

DEPLOY_USER="pvedeploy"

CONFIG_DIR="/etc/proxmox-deployer"
SSH_DIR="${CONFIG_DIR}/ssh"
SECRETS_DIR="${CONFIG_DIR}/secrets"

RUNTIME_DIR="/var/lib/proxmox-deployer"
REPO_DIR="${RUNTIME_DIR}/repo"

STATE_DIR="${RUNTIME_DIR}/state"
STATE_FILE="${STATE_DIR}/state.json"
VERSION_FILE="${STATE_DIR}/version"
LAST_RUN_FILE="${STATE_DIR}/last-run.json"
LOCK_FILE="/run/lock/proxmox-bootstrap-init.lock"

LOG_DIR="/var/log/proxmox-deployer"
BOOTSTRAP_LOG_DIR="/var/log/proxmox-bootstrap"

BACKUP_ROOT="/var/backups/proxmox-bootstrap"
SECRETS_BACKUP_ROOT="/var/backups/proxmox-secrets"

KEY_FILE="${SSH_DIR}/github_proxmox_repo_ed25519"
SSH_CONFIG="${SSH_DIR}/config"
KNOWN_HOSTS="${SSH_DIR}/known_hosts"

TEMPLATE_VMID=9000
TEMPLATE_NAME="tpl-debian13"
MANAGED_POOL="managed"
BRIDGE="vmbr0"

# Минимальный безопасный запас для первого создания Debian template.
TEMPLATE_MIN_LOCAL_BYTES=$((2 * 1024 * 1024 * 1024))
TEMPLATE_MIN_DISK_STORAGE_BYTES=$((18 * 1024 * 1024 * 1024))

UPDATE_SYSTEM=0
WARN_COUNT=0
REPO_REVISION=""
BOOTSTRAP_RUNNING=0

# Учётная запись для человека и локальных инструментов, работающих напрямую с PVE.
HOST_PVE_USER="deployer@pve"
HOST_PVE_TOKEN_NAME="host-deploy"
HOST_PVE_TOKEN="${HOST_PVE_USER}!${HOST_PVE_TOKEN_NAME}"
HOST_TOKEN_FILE="${SECRETS_DIR}/host-deploy.token"

# Учётная запись именно для контура AI / Proximo.
AI_PVE_USER="ai-agent@pve"
AI_PVE_TOKEN_NAME="infra"
AI_PVE_TOKEN="${AI_PVE_USER}!${AI_PVE_TOKEN_NAME}"
AI_TOKEN_FILE="${SECRETS_DIR}/ai-agent-infra.token"

# Существующая схема ролей проекта.
ROLE_CLONE="AICloneSource"
ROLE_GUEST="AIManagedGuest"
ROLE_NETWORK="AINetworkUse"
ROLE_STORAGE="AIStorage"
ROLE_POOL="AIManagedPool"

ROLE_CLONE_PRIVS="VM.Audit VM.Clone"
ROLE_GUEST_PRIVS="VM.Allocate VM.Audit VM.Backup VM.Clone VM.Config.CDROM VM.Config.Cloudinit VM.Config.CPU VM.Config.Disk VM.Config.HWType VM.Config.Memory VM.Config.Network VM.Config.Options VM.Console VM.GuestAgent.Audit VM.PowerMgmt VM.Snapshot VM.Snapshot.Rollback"
ROLE_NETWORK_PRIVS="SDN.Use"
ROLE_STORAGE_PRIVS="Datastore.AllocateSpace Datastore.Audit"
ROLE_POOL_PRIVS="Pool.Allocate Pool.Audit"

log()  { printf '\n==> %s\n' "$*"; }
ok()   { printf '[ОК] %s\n' "$*"; }
warn() {
    WARN_COUNT=$((WARN_COUNT + 1))
    printf '[ПРЕДУПРЕЖДЕНИЕ] %s\n' "$*" >&2
}
die() {
    local message=$*
    printf '\nОШИБКА: %s\n' "$message" >&2
    if (( BOOTSTRAP_RUNNING )); then
        trap - ERR
        write_state "failed" "$REPO_REVISION" >/dev/null 2>&1 || true
    fi
    exit 1
}

usage() {
    cat <<'USAGE'
Использование:
  init-pve.sh [--update-system] [--help]

Приватная полная инициализация нового или частично настроенного Proxmox VE.
Обычно запускается автоматически публичной Stage 0 после получения read-only
доступа к zsergeyru/proxmox.

Параметры:
  --update-system  после настройки репозиториев выполнить apt full-upgrade
  -h, --help       показать эту справку
USAGE
}

while (($#)); do
    case "$1" in
        --update-system) UPDATE_SYSTEM=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "Неизвестный параметр: $1" ;;
    esac
    shift
done

on_error() {
    local rc=$?
    trap - ERR
    if (( BOOTSTRAP_RUNNING )); then
        write_state "failed" "$REPO_REVISION" >/dev/null 2>&1 || true
    fi
    printf '\nПриватная инициализация PVE аварийно остановлена. Код возврата: %s.\n' "$rc" >&2
    exit "$rc"
}
trap on_error ERR

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "Не найдена обязательная команда: $1"
}

setup_bootstrap_log() {
    mkdir -p "$BOOTSTRAP_LOG_DIR"
    chmod 0750 "$BOOTSTRAP_LOG_DIR"
    touch "$BOOTSTRAP_LOG_DIR/init-pve.log"
    chmod 0600 "$BOOTSTRAP_LOG_DIR/init-pve.log"
    exec > >(tee -a "$BOOTSTRAP_LOG_DIR/init-pve.log") 2>&1
}

acquire_bootstrap_lock() {
    require_cmd flock
    install -d -m 0755 /run/lock
    exec 9>"$LOCK_FILE"
    flock -n 9 || die "Другой экземпляр приватного init-pve.sh уже выполняется. Параллельный запуск запрещён."
    ok "Получена эксклюзивная блокировка bootstrap"
}

write_state() {
    local status=$1
    local revision=${2:-}
    local now
    now="$(date --iso-8601=seconds)"

    mkdir -p "$STATE_DIR"
    chmod 0750 "$STATE_DIR"

    cat >"$STATE_FILE" <<EOF_STATE
{
  "bootstrap_version": ${BOOTSTRAP_VERSION},
  "stage": "private",
  "status": "${status}",
  "repository_revision": "${revision}",
  "private_repo_checkout_exists": $( [[ -d "$REPO_DIR/.git" ]] && echo true || echo false ),
  "host_deploy_secret_exists": $( [[ -f "$HOST_TOKEN_FILE" ]] && echo true || echo false ),
  "ai_infra_secret_exists": $( [[ -f "$AI_TOKEN_FILE" ]] && echo true || echo false ),
  "template_vmid": ${TEMPLATE_VMID},
  "warnings": ${WARN_COUNT}
}
EOF_STATE

    cat >"$LAST_RUN_FILE" <<EOF_LAST
{
  "timestamp": "${now}",
  "stage": "private",
  "result": "${status}",
  "repository_revision": "${revision}",
  "warnings": ${WARN_COUNT}
}
EOF_LAST

    printf '%s\n' "$BOOTSTRAP_VERSION" >"$VERSION_FILE"
    chmod 0644 "$STATE_FILE" "$LAST_RUN_FILE" "$VERSION_FILE"
}

# =============================================================================
# Предварительные проверки
# =============================================================================

check_root_and_pve() {
    [[ $EUID -eq 0 ]] || die "Запустите скрипт от root на хосте Proxmox"

    for cmd in \
        pveversion qm pct pvesh pveum pvesm \
        ip systemctl apt-get getent groupadd useradd; do
        require_cmd "$cmd"
    done

    pveversion >/dev/null
    ok "Обнаружен Proxmox: $(pveversion | head -n1)"

    local codename
    codename="$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-}")"
    [[ "$codename" == "trixie" ]] \
        || die "Для текущей инициализации ожидается Debian trixie, обнаружено: '${codename:-неизвестно}'"

    [[ -e /dev/kvm ]] || die "Не найден /dev/kvm: аппаратная виртуализация/KVM недоступна"
    ok "KVM доступен"

    ip link show "$BRIDGE" >/dev/null 2>&1 || die "Не найден обязательный сетевой мост ${BRIDGE}"
    ok "Сетевой мост ${BRIDGE} найден"

    pvesm status --storage local >/dev/null 2>&1 || die "Хранилище 'local' недоступно"
    pvesm status --storage local-lvm >/dev/null 2>&1 || die "Хранилище 'local-lvm' недоступно"
    ok "Хранилища local и local-lvm доступны"

    if pct config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        die "VMID ${TEMPLATE_VMID} уже занят LXC-контейнером; скрипт не будет его изменять"
    fi

    if qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        local name template_flag protection_flag
        name="$(qm config "$TEMPLATE_VMID" | awk -F': ' '$1=="name" {print $2}')"
        template_flag="$(qm config "$TEMPLATE_VMID" | awk -F': ' '$1=="template" {print $2}')"
        protection_flag="$(qm config "$TEMPLATE_VMID" | awk -F': ' '$1=="protection" {print $2}')"
        if [[ "$name" != "$TEMPLATE_NAME" || "$template_flag" != "1" ]]; then
            die "VMID ${TEMPLATE_VMID} уже существует, но это не ожидаемый шаблон ${TEMPLATE_NAME}; перезапись запрещена"
        fi
        if [[ "$protection_flag" != "1" ]]; then
            qm set "$TEMPLATE_VMID" --protection 1
            protection_flag="$(qm config "$TEMPLATE_VMID" | awk -F': ' '$1=="protection" {print $2}')"
            [[ "$protection_flag" == "1" ]] \
                || die "Не удалось установить protection=1 для шаблона ${TEMPLATE_VMID} ${TEMPLATE_NAME}"
            ok "Для существующего шаблона ${TEMPLATE_VMID} автоматически включён protection=1"
        else
            ok "Защищённый шаблон ${TEMPLATE_VMID} уже существует"
        fi
    fi
}

snapshot_host_config() {
    local stamp dst f rel
    stamp="$(date +%Y%m%d-%H%M%S)"
    dst="${BACKUP_ROOT}/${stamp}"

    mkdir -p "$dst/files" "$dst/diagnostics"
    chmod 0700 "$dst"

    for f in \
        /etc/network/interfaces \
        /etc/hosts \
        /etc/hostname \
        /etc/resolv.conf \
        /etc/pve/storage.cfg \
        /etc/pve/user.cfg \
        /etc/pve/datacenter.cfg; do
        if [[ -e "$f" ]]; then
            rel="${f#/}"
            mkdir -p "$dst/files/$(dirname "$rel")"
            cp -a "$f" "$dst/files/$rel"
        fi
    done

    if [[ -d /etc/apt/sources.list.d ]]; then
        mkdir -p "$dst/files/etc/apt"
        cp -a /etc/apt/sources.list "$dst/files/etc/apt/" 2>/dev/null || true
        cp -a /etc/apt/sources.list.d "$dst/files/etc/apt/" 2>/dev/null || true
    fi

    pveversion -v >"$dst/diagnostics/pveversion.txt" 2>&1 || true
    pvesm status >"$dst/diagnostics/storage-status.txt" 2>&1 || true
    pvesm status --content snippets >"$dst/diagnostics/storage-snippets.txt" 2>&1 || true
    pveum user list >"$dst/diagnostics/users.txt" 2>&1 || true
    pveum role list >"$dst/diagnostics/roles.txt" 2>&1 || true
    pveum acl list >"$dst/diagnostics/acl.txt" 2>&1 || true
    pveum pool list >"$dst/diagnostics/pools.txt" 2>&1 || true
    ip addr >"$dst/diagnostics/ip-addr.txt" 2>&1 || true
    ip route >"$dst/diagnostics/ip-route.txt" 2>&1 || true

    ok "Сохранён снимок конфигурации хоста: $dst"
}

# =============================================================================
# Репозитории, пакеты, сеть и storage
# =============================================================================

configure_apt() {
    log "Настройка репозитория pve-no-subscription"

    install -d -m 0755 /etc/apt/sources.list.d
    cat >/etc/apt/sources.list.d/pve-no-subscription.sources <<'EOF_APT'
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF_APT

    local f
    for f in \
        /etc/apt/sources.list.d/pve-enterprise.list \
        /etc/apt/sources.list.d/pve-enterprise.sources; do
        if [[ -f "$f" ]]; then
            mv -f "$f" "${f}.disabled"
            ok "Отключён репозиторий enterprise: $f"
        fi
    done

    apt-get update
    if (( UPDATE_SYSTEM )); then
        log "Выполняется явно запрошенное полное обновление системы"
        DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade
    fi
}

install_packages() {
    log "Установка обязательных пакетов инициализации и администрирования"

    local packages=(
        git openssh-client python3 python3-yaml curl jq ca-certificates
        mc htop tmux smartmontools lm-sensors
    )

    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${packages[@]}"

    for cmd in git ssh ssh-keygen python3 curl jq runuser; do
        require_cmd "$cmd"
    done

    ok "Обязательные пакеты установлены"
}

check_time_dns_network() {
    log "Проверка времени, DNS и исходящего подключения"

    date --iso-8601=seconds

    if command -v timedatectl >/dev/null 2>&1; then
        timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -qx yes \
            || warn "Синхронизация времени NTP пока не подтверждена"
    fi

    getent ahosts github.com >/dev/null || die "Не работает DNS-разрешение имени github.com"
    getent ahosts download.proxmox.com >/dev/null || die "Не работает DNS-разрешение имени download.proxmox.com"

    curl -fsS --connect-timeout 10 -o /dev/null https://github.com/ \
        || die "Нет HTTPS-доступа к GitHub"
    curl -fsS --connect-timeout 10 -o /dev/null https://download.proxmox.com/ \
        || die "Нет HTTPS-доступа к download.proxmox.com"

    ok "DNS и исходящий HTTPS работают"
}

storage_available_bytes() {
    local storage=$1
    pvesm status --storage "$storage" --output-format json \
        | jq -r --arg storage "$storage" '.[] | select(.storage == $storage) | (.avail // empty)' \
        | head -n1
}

check_template_capacity() {
    if qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        return
    fi

    log "Проверка свободного места для создания Debian-шаблона"

    local local_avail disk_avail
    local_avail="$(storage_available_bytes local)"
    disk_avail="$(storage_available_bytes local-lvm)"

    [[ "$local_avail" =~ ^[0-9]+$ ]] \
        || die "Не удалось определить доступное место на storage local"
    [[ "$disk_avail" =~ ^[0-9]+$ ]] \
        || die "Не удалось определить доступное место на storage local-lvm"

    (( local_avail >= TEMPLATE_MIN_LOCAL_BYTES )) \
        || die "Недостаточно свободного места на local для образа Debian: доступно $((local_avail / 1024 / 1024)) MiB, требуется минимум $((TEMPLATE_MIN_LOCAL_BYTES / 1024 / 1024)) MiB"
    (( disk_avail >= TEMPLATE_MIN_DISK_STORAGE_BYTES )) \
        || die "Недостаточно свободного места на local-lvm для template disk: доступно $((disk_avail / 1024 / 1024 / 1024)) GiB, требуется минимум $((TEMPLATE_MIN_DISK_STORAGE_BYTES / 1024 / 1024 / 1024)) GiB"

    ok "Свободного места для создания template достаточно"
}

check_template_source() {
    if qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        return
    fi

    log "Проверка источника облачного образа Debian"
    getent ahosts cloud.debian.org >/dev/null || die "Не работает DNS-разрешение имени cloud.debian.org"
    curl -fsS --connect-timeout 10 -o /dev/null \
        https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS \
        || die "Нет HTTPS-доступа к облачным образам Debian"
    ok "Источник облачного образа Debian доступен"
}

ensure_snippets() {
    log "Проверка поддержки snippets в хранилище local"

    local content new_content
    content="$(pvesm config local | awk -F' ' '$1=="content" {print $2}')"

    if tr ',' '\n' <<<"$content" | grep -qx snippets; then
        ok "В local уже разрешён тип содержимого snippets"
        return
    fi

    [[ -n "$content" ]] || die "Не удалось определить текущие типы содержимого хранилища local"
    new_content="${content},snippets"
    pvesm set local --content "$new_content"

    content="$(pvesm config local | awk -F' ' '$1=="content" {print $2}')"
    tr ',' '\n' <<<"$content" | grep -qx snippets \
        || die "После изменения хранилище local по-прежнему не поддерживает snippets"

    ok "В local добавлен snippets без удаления существующих типов содержимого"
}

# =============================================================================
# Каноническая файловая структура и принятие Deploy Key от Stage 0
# =============================================================================

ensure_runtime_layout() {
    log "Подготовка каталогов и локального пользователя pvedeploy"

    if ! getent group "$DEPLOY_USER" >/dev/null 2>&1; then
        groupadd --system "$DEPLOY_USER"
        ok "Создана Linux-группа ${DEPLOY_USER}"
    fi

    if ! id "$DEPLOY_USER" >/dev/null 2>&1; then
        useradd --system --gid "$DEPLOY_USER" --create-home \
            --home-dir /var/lib/pvedeploy --shell /bin/bash "$DEPLOY_USER"
        ok "Создан Linux-пользователь ${DEPLOY_USER}"
    else
        ok "Linux-пользователь ${DEPLOY_USER} уже существует"
    fi

    install -d -o root -g root -m 0755 "$CONFIG_DIR"
    install -d -o root -g "$DEPLOY_USER" -m 0750 "$SSH_DIR"
    install -d -o root -g "$DEPLOY_USER" -m 0710 "$SECRETS_DIR"
    install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0750 \
        "$RUNTIME_DIR" "$RUNTIME_DIR/state" "$RUNTIME_DIR/cache"
    install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0750 "$LOG_DIR" "$LOG_DIR/audit"
    install -d -o root -g root -m 0700 "$BACKUP_ROOT" "$SECRETS_BACKUP_ROOT"
    install -d -o root -g root -m 0755 /usr/local/sbin

    if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
        cat >"$CONFIG_DIR/config.yaml" <<EOF_CONFIG
repo: zsergeyru/proxmox
branch: ${PRIVATE_BRANCH}
checkout: ${REPO_DIR}
managed_pool: ${MANAGED_POOL}
host_deploy_identity: ${HOST_PVE_TOKEN}
ai_infra_identity: ${AI_PVE_TOKEN}
template_vmid: ${TEMPLATE_VMID}
EOF_CONFIG
        chmod 0644 "$CONFIG_DIR/config.yaml"
        ok "Создан ${CONFIG_DIR}/config.yaml"
    else
        ok "Файл ${CONFIG_DIR}/config.yaml уже существует и не перезаписывается"
    fi
}

prepare_canonical_github_access() {
    log "Принятие read-only Deploy Key из публичной Stage 0"

    if [[ ! -f "$KEY_FILE" ]]; then
        [[ -f "$STAGE0_KEY_FILE" && -f "$STAGE0_KEY_PUB_FILE" ]] \
            || die "Канонический GitHub Deploy Key отсутствует и Stage 0 не передала временный ключ. Сначала запустите публичный proxmox-bootstrap/init-pve.sh."

        install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0600 "$STAGE0_KEY_FILE" "$KEY_FILE"
        install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0644 "$STAGE0_KEY_PUB_FILE" "${KEY_FILE}.pub"
        ok "Deploy Key перенесён в каноническое хранилище"
    else
        chown "$DEPLOY_USER:$DEPLOY_USER" "$KEY_FILE"
        chmod 0600 "$KEY_FILE"
        [[ ! -f "${KEY_FILE}.pub" ]] || {
            chown "$DEPLOY_USER:$DEPLOY_USER" "${KEY_FILE}.pub"
            chmod 0644 "${KEY_FILE}.pub"
        }
        ok "Канонический GitHub Deploy Key уже существует"
    fi

    if [[ -f "$STAGE0_KNOWN_HOSTS" ]]; then
        install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0644 "$STAGE0_KNOWN_HOSTS" "$KNOWN_HOSTS"
    elif [[ ! -f "$KNOWN_HOSTS" ]]; then
        local tmp_hosts
        tmp_hosts="$(mktemp)"
        curl -fsSL https://api.github.com/meta \
            | jq -r '.ssh_keys[] | "github.com " + .' >"$tmp_hosts"
        [[ -s "$tmp_hosts" ]] || {
            rm -f "$tmp_hosts"
            die "Не удалось получить SSH-ключи хоста GitHub через api.github.com/meta"
        }
        install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0644 "$tmp_hosts" "$KNOWN_HOSTS"
        rm -f "$tmp_hosts"
    fi

    cat >"$SSH_CONFIG" <<EOF_SSH
Host github.com
    HostName github.com
    User git
    IdentityFile ${KEY_FILE}
    IdentitiesOnly yes
    UserKnownHostsFile ${KNOWN_HOSTS}
    StrictHostKeyChecking yes
EOF_SSH
    chown "$DEPLOY_USER:$DEPLOY_USER" "$SSH_CONFIG"
    chmod 0600 "$SSH_CONFIG"
}

git_as_deployer() {
    runuser -u "$DEPLOY_USER" -- env GIT_SSH_COMMAND="ssh -F ${SSH_CONFIG}" git "$@"
}

verify_private_repo_access() {
    git_as_deployer ls-remote "$PRIVATE_REPO" HEAD >/dev/null 2>&1 \
        || die "Канонический Deploy Key не даёт read-only доступ к ${PRIVATE_REPO}"
    ok "Read-only доступ к приватному GitHub-репозиторию подтверждён"
}

sync_private_repo() {
    log "Синхронизация приватного источника истины"

    if [[ ! -d "$REPO_DIR/.git" ]]; then
        rm -rf "$REPO_DIR"
        git_as_deployer clone --depth 1 --branch "$PRIVATE_BRANCH" "$PRIVATE_REPO" "$REPO_DIR"
    else
        local origin_url
        chown -R "$DEPLOY_USER:$DEPLOY_USER" "$REPO_DIR"
        origin_url="$(git_as_deployer -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)"
        [[ "$origin_url" == "$PRIVATE_REPO" ]] \
            || die "Существующий checkout ${REPO_DIR} имеет неожиданный origin '${origin_url:-не задан}'. Ожидается '${PRIVATE_REPO}'. Автоматическая подмена origin запрещена."
        ok "Origin существующего private checkout соответствует каноническому репозиторию"

        git_as_deployer -C "$REPO_DIR" fetch --depth 1 origin "$PRIVATE_BRANCH"
        git_as_deployer -C "$REPO_DIR" reset --hard FETCH_HEAD
        git_as_deployer -C "$REPO_DIR" clean -ffd
    fi

    REPO_REVISION="$(git_as_deployer -C "$REPO_DIR" rev-parse HEAD)"
    printf '%s\n' "$REPO_REVISION" >"$RUNTIME_DIR/state/last-revision"
    chown "$DEPLOY_USER:$DEPLOY_USER" "$RUNTIME_DIR/state/last-revision"
    ok "Приватный репозиторий синхронизирован: ${REPO_REVISION}"
}

# =============================================================================
# managed, роли, пользователи, токены и ACL
# =============================================================================

ensure_managed_pool() {
    log "Проверка пула ресурсов ${MANAGED_POOL}"
    if pveum pool list --output-format json 2>/dev/null \
        | jq -e --arg p "$MANAGED_POOL" '.[] | select(.poolid == $p)' >/dev/null; then
        ok "Пул ресурсов ${MANAGED_POOL} уже существует"
    else
        pveum pool add "$MANAGED_POOL" --comment "Рабочая зона автоматизации проекта Proxmox"
        ok "Создан пул ресурсов ${MANAGED_POOL}"
    fi
}

priv_lines() {
    printf '%s\n' "$1" | tr ', ' '\n\n' | sed '/^$/d' | sort -u
}

join_lines() {
    if [[ -z "${1:-}" ]]; then printf 'нет'; else printf '%s\n' "$1" | paste -sd, -; fi
}

ensure_role() {
    local role=$1 expected_raw=$2
    local role_json actual_raw expected actual missing extra missing_raw missing_after

    role_json="$(pveum role list --output-format json)"
    if ! jq -e --arg role "$role" '.[] | select(.roleid == $role)' <<<"$role_json" >/dev/null; then
        pveum role add "$role" --privs "$expected_raw"
        ok "Создана роль ${role}"
        return
    fi

    actual_raw="$(jq -r --arg role "$role" '.[] | select(.roleid == $role) | (.privs // "")' \
        <<<"$role_json" | head -n1)"
    expected="$(priv_lines "$expected_raw")"
    actual="$(priv_lines "$actual_raw")"
    missing="$(comm -23 <(printf '%s\n' "$expected") <(printf '%s\n' "$actual") || true)"
    extra="$(comm -13 <(printf '%s\n' "$expected") <(printf '%s\n' "$actual") || true)"

    if [[ -n "$missing" ]]; then
        missing_raw="$(printf '%s\n' "$missing" | paste -sd' ' -)"
        pveum role modify "$role" --append 1 --privs "$missing_raw"
        ok "Роль ${role} дополнена недостающими privileges: $(join_lines "$missing")"

        role_json="$(pveum role list --output-format json)"
        actual_raw="$(jq -r --arg role "$role" '.[] | select(.roleid == $role) | (.privs // "")' \
            <<<"$role_json" | head -n1)"
        actual="$(priv_lines "$actual_raw")"
        missing_after="$(comm -23 <(printf '%s\n' "$expected") <(printf '%s\n' "$actual") || true)"
        [[ -z "$missing_after" ]] \
            || die "После дополнения роли ${role} по-прежнему отсутствуют privileges: $(join_lines "$missing_after")"
    else
        ok "Роль ${role} уже содержит все требуемые privileges"
    fi

    if [[ -n "$extra" ]]; then
        printf '[ИНФО] Роль %s содержит дополнительные privileges, они сохранены без изменений: %s\n' \
            "$role" "$(join_lines "$extra")"
    fi
}

ensure_roles() {
    log "Проверка ролей Proxmox"
    ensure_role "$ROLE_CLONE" "$ROLE_CLONE_PRIVS"
    ensure_role "$ROLE_GUEST" "$ROLE_GUEST_PRIVS"
    ensure_role "$ROLE_NETWORK" "$ROLE_NETWORK_PRIVS"
    ensure_role "$ROLE_STORAGE" "$ROLE_STORAGE_PRIVS"
    ensure_role "$ROLE_POOL" "$ROLE_POOL_PRIVS"
}

ensure_pve_user() {
    local userid=$1 comment=$2 users_json enabled
    users_json="$(pveum user list --output-format json)"

    if jq -e --arg id "$userid" '.[] | select(.userid == $id)' <<<"$users_json" >/dev/null; then
        enabled="$(jq -r --arg id "$userid" '.[] | select(.userid == $id) | (.enable // 1)' \
            <<<"$users_json" | head -n1)"
        [[ "$enabled" != "0" ]] \
            || die "Пользователь PVE ${userid} существует, но отключён. Bootstrap не включает существующую учётную запись автоматически, чтобы не отменить осознанную блокировку. Сначала проверьте причину и включите пользователя вручную, если это действительно требуется."
        ok "Пользователь PVE ${userid} уже существует и включён"
    else
        pveum user add "$userid" --comment "$comment" --enable 1
        ok "Создан пользователь PVE ${userid}"
    fi
}

token_entry() {
    local userid=$1 token_name=$2
    pveum user token list "$userid" --output-format json 2>/dev/null \
        | jq -c --arg id "$token_name" '.[] | select(.tokenid == $id)' | head -n1
}

write_token_secret() {
    local file=$1 full_token=$2 secret=$3 group=$4 mode=$5 old_umask
    old_umask="$(umask)"
    umask 077
    printf 'token_id=%s\ntoken_secret=%s\n' "$full_token" "$secret" >"$file"
    umask "$old_umask"
    chown root:"$group" "$file"
    chmod "$mode" "$file"
}

ensure_token() {
    local userid=$1 token_name=$2 full_token=$3 secret_file=$4
    local file_group=$5 file_mode=$6 comment=$7
    local entry privsep out secret returned_id

    entry="$(token_entry "$userid" "$token_name" || true)"
    if [[ -n "$entry" ]]; then
        privsep="$(jq -r '.privsep // 1' <<<"$entry")"
        if [[ "$privsep" != "1" ]]; then
            warn "API-токен ${full_token} уже существует с privsep=${privsep}; настройка не изменяется автоматически, чтобы не ограничить уже работающий доступ"
            printf '       При privsep=0 токен использует права базового пользователя %s.\n' "$userid"
            printf '       Отдельные ACL токена в этом режиме не служат дополнительным ограничителем.\n'
            printf '       Если перевод на privsep=1 понадобится, это должно быть отдельным осознанным действием после проверки текущих потребителей токена.\n'
        else
            ok "API-токен ${full_token} использует privsep=1"
        fi

        [[ -f "$secret_file" ]] \
            || die "API-токен ${full_token} существует, но локальный файл секрета ${secret_file} отсутствует. Требуется явная ротация или восстановление учётных данных."

        chown root:"$file_group" "$secret_file"
        chmod "$file_mode" "$secret_file"
        ok "API-токен ${full_token} уже существует, локальный секрет найден"
        return
    fi

    [[ ! -f "$secret_file" ]] \
        || warn "Файл ${secret_file} существует, но API-токен ${full_token} отсутствует; будет создан новый токен и локальное значение будет заменено"

    out="$(pveum user token add "$userid" "$token_name" --privsep 1 --comment "$comment" --output-format json)"
    secret="$(jq -r '.value // .data.value // empty' <<<"$out")"
    returned_id="$(jq -r '."full-tokenid" // .data."full-tokenid" // empty' <<<"$out")"

    [[ -n "$secret" ]] || die "Proxmox создал ${full_token}, но одноразовый секрет не удалось разобрать"
    [[ -z "$returned_id" || "$returned_id" == "$full_token" ]] \
        || die "Proxmox вернул неожиданный идентификатор API-токена: ${returned_id}"

    write_token_secret "$secret_file" "$full_token" "$secret" "$file_group" "$file_mode"
    unset secret out
    ok "Создан API-токен ${full_token} с privsep=1, секрет сохранён на PVE"
}

acl_entry() {
    local path=$1 type=$2 ugid=$3 role=$4
    pveum acl list --output-format json \
        | jq -c --arg path "$path" --arg type "$type" --arg ugid "$ugid" --arg role "$role" \
            '.[] | select(.path == $path and .type == $type and .ugid == $ugid and .roleid == $role)' \
        | head -n1
}

ensure_acl_entry() {
    local path=$1 type=$2 ugid=$3 role=$4
    local entry propagate option label

    entry="$(acl_entry "$path" "$type" "$ugid" "$role" || true)"
    if [[ -n "$entry" ]]; then
        propagate="$(jq -r '.propagate // 1' <<<"$entry")"
        if [[ "$propagate" == "1" ]]; then
            ok "ACL уже существует: ${path}, ${type}=${ugid}, роль=${role}"
        else
            warn "ACL ${path}, ${type}=${ugid}, роль=${role} существует с propagate=${propagate}; скрипт его не изменяет"
        fi
        return
    fi

    case "$type" in
        user) option="--users"; label="пользователь" ;;
        token) option="--tokens"; label="API-токен" ;;
        *) die "Неподдерживаемый тип субъекта ACL: $type" ;;
    esac

    pveum acl modify "$path" "$option" "$ugid" --roles "$role" --propagate 1
    ok "Добавлен ACL: ${path}, ${label}=${ugid}, роль=${role}"
}

ensure_identity_acls() {
    local userid=$1 tokenid=$2 principal_type principal

    # Для privsep=1 одинаковые ACL нужны базовому пользователю и самому API-токену.
    # Если существующий токен имеет privsep=0, bootstrap всё равно подготавливает
    # его ACL, но не меняет режим токена автоматически.
    for principal_type in user token; do
        if [[ "$principal_type" == "user" ]]; then principal="$userid"; else principal="$tokenid"; fi

        # managed — основная write-zone обычных VM/LXC.
        ensure_acl_entry "/pool/${MANAGED_POOL}" "$principal_type" "$principal" "$ROLE_GUEST"
        # Pool.Allocate разрешает явное изменение состава managed.
        ensure_acl_entry "/pool/${MANAGED_POOL}" "$principal_type" "$principal" "$ROLE_POOL"
        # 9000 доступен отдельно только как источник clone.
        ensure_acl_entry "/vms/${TEMPLATE_VMID}" "$principal_type" "$principal" "$ROLE_CLONE"
        ensure_acl_entry "/storage/local" "$principal_type" "$principal" "$ROLE_STORAGE"
        ensure_acl_entry "/storage/local-lvm" "$principal_type" "$principal" "$ROLE_STORAGE"
        ensure_acl_entry "/sdn/zones/localnetwork/${BRIDGE}" "$principal_type" "$principal" "$ROLE_NETWORK"
    done
}

verify_permission_set() {
    local full_token=$1 permissions_json=$2 path=$3 required_raw=$4
    local priv missing=""

    while IFS= read -r priv; do
        [[ -n "$priv" ]] || continue
        if ! jq -e --arg path "$path" --arg priv "$priv" \
            '((.[$path] // {}) | has($priv))' <<<"$permissions_json" >/dev/null; then
            missing+="${priv}"$'\n'
        fi
    done < <(priv_lines "$required_raw")

    [[ -z "$missing" ]] \
        || die "API-токен ${full_token} не имеет ожидаемых effective permissions на ${path}: $(join_lines "${missing%$'\n'}")"
}

verify_effective_permissions() {
    local full_token=$1 permissions_json

    permissions_json="$(pveum user permissions "$full_token" --output-format json)" \
        || die "Не удалось получить effective permissions для ${full_token}"

    verify_permission_set "$full_token" "$permissions_json" "/pool/${MANAGED_POOL}" \
        "${ROLE_GUEST_PRIVS} ${ROLE_POOL_PRIVS}"
    verify_permission_set "$full_token" "$permissions_json" "/vms/${TEMPLATE_VMID}" \
        "$ROLE_CLONE_PRIVS"
    verify_permission_set "$full_token" "$permissions_json" "/storage/local" \
        "$ROLE_STORAGE_PRIVS"
    verify_permission_set "$full_token" "$permissions_json" "/storage/local-lvm" \
        "$ROLE_STORAGE_PRIVS"
    verify_permission_set "$full_token" "$permissions_json" "/sdn/zones/localnetwork/${BRIDGE}" \
        "$ROLE_NETWORK_PRIVS"

    ok "Effective permissions API-токена ${full_token} соответствуют требуемой модели"
}

verify_token_api_auth() {
    local full_token=$1 secret_file=$2
    local stored_id secret header_file old_umask

    stored_id="$(sed -n 's/^token_id=//p' "$secret_file" | head -n1)"
    secret="$(sed -n 's/^token_secret=//p' "$secret_file" | head -n1)"

    [[ "$stored_id" == "$full_token" ]] \
        || die "В ${secret_file} записан token_id '${stored_id:-не задан}', ожидается '${full_token}'"
    [[ -n "$secret" ]] || die "В ${secret_file} отсутствует token_secret"

    old_umask="$(umask)"
    umask 077
    header_file="$(mktemp "${SECRETS_DIR}/.api-check.XXXXXX")"
    umask "$old_umask"
    printf 'Authorization: PVEAPIToken=%s=%s\n' "$full_token" "$secret" >"$header_file"
    chmod 0600 "$header_file"

    if ! curl --fail --silent --show-error --insecure \
        --connect-timeout 10 --max-time 20 \
        --header "@${header_file}" \
        https://127.0.0.1:8006/api2/json/version \
        | jq -e '(.data.version // .data.release // empty) != ""' >/dev/null; then
        rm -f "$header_file"
        unset secret
        die "API-токен ${full_token} не прошёл локальную проверку авторизации. Возможен устаревший secret или другая проблема credential."
    fi

    rm -f "$header_file"
    unset secret
    ok "API-токен ${full_token} успешно авторизуется в локальном Proxmox API"
}

verify_pve_identity() {
    local full_token=$1 secret_file=$2
    verify_effective_permissions "$full_token"
    verify_token_api_auth "$full_token" "$secret_file"
}

ensure_pve_identities() {
    log "Проверка служебных учётных записей, API-токенов и ACL Proxmox"

    ensure_pve_user "$HOST_PVE_USER" \
        "Учётная запись человека и локальных инструментов PVE для прямого развёртывания"
    ensure_pve_user "$AI_PVE_USER" \
        "Учётная запись контура AI, используемая Proximo"

    ensure_token "$HOST_PVE_USER" "$HOST_PVE_TOKEN_NAME" "$HOST_PVE_TOKEN" \
        "$HOST_TOKEN_FILE" "$DEPLOY_USER" 0640 \
        "API-токен для deploy-guest и прямой локальной автоматизации PVE"
    ensure_token "$AI_PVE_USER" "$AI_PVE_TOKEN_NAME" "$AI_PVE_TOKEN" \
        "$AI_TOKEN_FILE" root 0600 \
        "Инфраструктурный API-токен для контура AI / Proximo"

    ensure_identity_acls "$HOST_PVE_USER" "$HOST_PVE_TOKEN"
    ensure_identity_acls "$AI_PVE_USER" "$AI_PVE_TOKEN"

    verify_pve_identity "$HOST_PVE_TOKEN" "$HOST_TOKEN_FILE"
    verify_pve_identity "$AI_PVE_TOKEN" "$AI_TOKEN_FILE"
}

# =============================================================================
# Template и локальные команды
# =============================================================================

ensure_template() {
    if pct config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        die "VMID ${TEMPLATE_VMID} занят LXC-контейнером; создание шаблона невозможно"
    fi

    if qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        local name template_flag protection_flag
        name="$(qm config "$TEMPLATE_VMID" | awk -F': ' '$1=="name" {print $2}')"
        template_flag="$(qm config "$TEMPLATE_VMID" | awk -F': ' '$1=="template" {print $2}')"
        protection_flag="$(qm config "$TEMPLATE_VMID" | awk -F': ' '$1=="protection" {print $2}')"
        [[ "$name" == "$TEMPLATE_NAME" && "$template_flag" == "1" ]] \
            || die "VMID ${TEMPLATE_VMID} существует, но не соответствует шаблону ${TEMPLATE_NAME}"
        if [[ "$protection_flag" != "1" ]]; then
            qm set "$TEMPLATE_VMID" --protection 1
            protection_flag="$(qm config "$TEMPLATE_VMID" | awk -F': ' '$1=="protection" {print $2}')"
            [[ "$protection_flag" == "1" ]] \
                || die "Не удалось установить protection=1 для шаблона ${TEMPLATE_VMID} ${TEMPLATE_NAME}"
            ok "Для существующего шаблона ${TEMPLATE_VMID} автоматически включён protection=1"
        else
            ok "Шаблон ${TEMPLATE_VMID} ${TEMPLATE_NAME} уже существует и защищён"
        fi
        return
    fi

    local builder="${REPO_DIR}/scripts/pve/create-template.sh"
    [[ -f "$builder" ]] || die "Не найден канонический скрипт создания шаблона: $builder"

    log "Создание защищённого Debian-шаблона ${TEMPLATE_VMID}"
    bash "$builder"

    qm config "$TEMPLATE_VMID" >/dev/null 2>&1 \
        || die "Скрипт создания шаблона завершился, но VMID ${TEMPLATE_VMID} не создан"

    local final_config
    final_config="$(qm config "$TEMPLATE_VMID")"
    grep -q "^name: ${TEMPLATE_NAME}$" <<<"$final_config" \
        || die "VMID ${TEMPLATE_VMID} создан, но имеет неожиданное имя"
    grep -q '^template: 1$' <<<"$final_config" \
        || die "VMID ${TEMPLATE_VMID} создан, но не является шаблоном Proxmox"
    grep -q '^protection: 1$' <<<"$final_config" \
        || die "VMID ${TEMPLATE_VMID} создан, но protection=1 не установлен"

    ok "Создан защищённый шаблон ${TEMPLATE_VMID} ${TEMPLATE_NAME}"
}

install_private_tooling() {
    log "Установка стабильных локальных команд PVE"

    cat >/usr/local/sbin/pve-bootstrap-status <<'EOF_STATUS'
#!/usr/bin/env bash
set -Eeuo pipefail
STATE=/var/lib/proxmox-deployer/state/state.json
[[ -f "$STATE" ]] || { echo "Файл состояния инициализации не найден: $STATE" >&2; exit 1; }
exec jq . "$STATE"
EOF_STATUS
    chmod 0755 /usr/local/sbin/pve-bootstrap-status

    local deployer_src="${REPO_DIR}/scripts/pve/deploy-guest.py"
    if [[ ! -f "$deployer_src" ]]; then
        warn "Исходный файл deploy-guest пока отсутствует: ${deployer_src}"
        return
    fi

    cat >/usr/local/sbin/deploy-guest <<EOF_DEPLOY
#!/usr/bin/env bash
set -Eeuo pipefail
SOURCE="${deployer_src}"
if [[ \$EUID -eq 0 ]]; then
    exec runuser -u ${DEPLOY_USER} -- /usr/bin/python3 "\$SOURCE" "\$@"
elif [[ \$(id -un) == "${DEPLOY_USER}" ]]; then
    exec /usr/bin/python3 "\$SOURCE" "\$@"
else
    echo "deploy-guest нужно запускать от root или ${DEPLOY_USER}" >&2
    exit 1
fi
EOF_DEPLOY
    chmod 0755 /usr/local/sbin/deploy-guest
    ok "Установлена обёртка /usr/local/sbin/deploy-guest"
}

report_status() {
    local ready=1

    printf '\nСОСТОЯНИЕ ПРИВАТНОЙ ИНИЦИАЛИЗАЦИИ PVE\n\n'
    printf '[ОК] Proxmox и KVM проверены\n'
    printf '[ОК] Эксклюзивная блокировка bootstrap используется\n'
    printf '[ОК] pve-no-subscription настроен\n'
    printf '[ОК] DNS и исходящий HTTPS работают\n'
    printf '[ОК] local/local-lvm/snippets подготовлены\n'
    printf '[ОК] pvedeploy и файловая структура подготовлены\n'
    printf '[ОК] Канонический read-only Deploy Key установлен\n'
    printf '[ОК] Приватный репозиторий синхронизирован и origin проверен\n'
    printf '[ОК] managed и роли Proxmox проверены\n'
    printf '[ОК] %s: effective permissions и API credential проверены\n' "$HOST_PVE_TOKEN"
    printf '[ОК] %s: effective permissions и API credential проверены\n' "$AI_PVE_TOKEN"

    if qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        printf '[ОК] Шаблон %s существует и protection проверен\n' "$TEMPLATE_VMID"
    else
        printf '[НЕТ] Шаблон %s отсутствует\n' "$TEMPLATE_VMID"
        ready=0
    fi

    if [[ -x /usr/local/sbin/deploy-guest ]]; then
        printf '[ОК] Команда deploy-guest установлена\n'
    else
        printf '[ОЖИДАНИЕ] deploy-guest пока не реализован\n'
        ready=0
    fi

    printf '\nРевизия приватного репозитория: %s\n' "$REPO_REVISION"
    printf 'Версия приватной инициализации: %s\n' "$BOOTSTRAP_VERSION"
    printf 'Предупреждений: %s\n' "$WARN_COUNT"

    if (( ready )); then
        if (( WARN_COUNT )); then
            printf '\nГОТОВО С ПРЕДУПРЕЖДЕНИЯМИ\n'
            write_state "ready-with-warnings" "$REPO_REVISION"
        else
            printf '\nГОТОВО\n'
            write_state "ready" "$REPO_REVISION"
        fi
    else
        printf '\nЧАСТИЧНО: основная инициализация выполнена, но один или несколько рабочих компонентов ещё не готовы.\n'
        write_state "partial" "$REPO_REVISION"
    fi
}

main() {
    [[ $EUID -eq 0 ]] || die "Запустите скрипт от root на хосте Proxmox"
    setup_bootstrap_log
    acquire_bootstrap_lock

    BOOTSTRAP_RUNNING=1
    write_state "running" "$REPO_REVISION"

    check_root_and_pve
    snapshot_host_config
    configure_apt
    install_packages
    check_time_dns_network
    ensure_snippets
    ensure_runtime_layout
    prepare_canonical_github_access
    verify_private_repo_access
    sync_private_repo
    ensure_managed_pool
    ensure_roles
    ensure_pve_identities
    check_template_capacity
    check_template_source
    ensure_template
    install_private_tooling
    report_status

    BOOTSTRAP_RUNNING=0
}

main "$@"
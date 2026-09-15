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

BOOTSTRAP_VERSION=12

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
PVE_GUEST_KEY="${SSH_DIR}/pve_guest_ed25519"
PVE_GUEST_PUB="${PVE_GUEST_KEY}.pub"
# Legacy name from BOOTSTRAP_VERSION=10. Used only for one-time migration
# without rotating a key that may already be authorized in existing guests.
LEGACY_GUEST_BOOTSTRAP_KEY="${SSH_DIR}/guest_bootstrap_ed25519"
LEGACY_GUEST_BOOTSTRAP_PUB="${LEGACY_GUEST_BOOTSTRAP_KEY}.pub"

TEMPLATE_VMID=9000
TEMPLATE_NAME="tpl-debian13"
TEMPLATE_VERSION=6
MANAGED_POOL="managed"
BRIDGE="vmbr0"
LXC_TEMPLATE_STORAGE="local"

# Минимальный безопасный запас для первого создания Debian template.
TEMPLATE_MIN_LOCAL_BYTES=$((2 * 1024 * 1024 * 1024))
TEMPLATE_MIN_DISK_STORAGE_BYTES=$((18 * 1024 * 1024 * 1024))

UPDATE_SYSTEM=0
WARN_COUNT=0
REPO_REVISION=""
BOOTSTRAP_RUNNING=0
API_HEADER_FILE=""
CANONICAL_KEY_DIFFERS_FROM_STAGE0=0
LXC_TEMPLATE_VOLUME=""

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

cleanup_api_header_file() {
    if [[ -n "${API_HEADER_FILE:-}" ]]; then
        rm -f -- "$API_HEADER_FILE" 2>/dev/null || true
        API_HEADER_FILE=""
    fi
}

on_error() {
    local rc=$?
    trap - ERR
    if (( BOOTSTRAP_RUNNING )); then
        write_state "failed" "$REPO_REVISION" >/dev/null 2>&1 || true
    fi
    printf '\nПриватная инициализация PVE аварийно остановлена. Код возврата: %s.\n' "$rc" >&2
    exit "$rc"
}

on_signal() {
    local rc=$1 signal_name=$2
    trap - ERR INT TERM
    cleanup_api_header_file
    if (( BOOTSTRAP_RUNNING )); then
        write_state "interrupted" "$REPO_REVISION" >/dev/null 2>&1 || true
    fi
    printf '\nПриватная инициализация PVE прервана сигналом %s.\n' "$signal_name" >&2
    exit "$rc"
}

trap on_error ERR
trap cleanup_api_header_file EXIT
trap 'on_signal 130 INT' INT
trap 'on_signal 143 TERM' TERM

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
  "pve_guest_key_exists": $( [[ -f "$PVE_GUEST_KEY" ]] && echo true || echo false ),
  "template_vmid": ${TEMPLATE_VMID},
  "template_version": ${TEMPLATE_VERSION},
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
        pveversion qm pct pvesh pveum pvesm pveam \
        ip systemctl apt-get getent groupadd useradd; do
        require_cmd "$cmd"
    done

    local pve_line codename
    pve_line="$(pveversion | head -n1)"
    [[ "$pve_line" =~ ^pve-manager/9\. ]] \
        || die "Для текущей инициализации ожидается Proxmox VE 9.x, обнаружено: ${pve_line:-неизвестно}"
    ok "Обнаружен Proxmox VE 9: ${pve_line}"

    codename="$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-}")"
    [[ "$codename" == "trixie" ]] \
        || die "Для текущей инициализации ожидается Debian trixie, обнаружено: '${codename:-неизвестно}'"

    [[ -e /dev/kvm ]] || die "Не найден /dev/kvm: аппаратная виртуализация/KVM недоступна"
    ok "KVM доступен"

    ip link show "$BRIDGE" >/dev/null 2>&1 || die "Не найден обязательный сетевой мост ${BRIDGE}"
    ok "Сетевой мост ${BRIDGE} найден"

    pvesm status --storage local >/dev/null 2>&1 || die "Хранилище 'local' не определено или недоступно"
    pvesm status --storage local-lvm >/dev/null 2>&1 || die "Хранилище 'local-lvm' не определено или недоступно"
    ok "Хранилища local и local-lvm найдены"

    if pct config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        die "VMID ${TEMPLATE_VMID} уже занят LXC-контейнером; скрипт не будет его изменять"
    fi

    if qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        local config name template_flag protection_flag description ciuser
        config="$(qm config "$TEMPLATE_VMID")"
        name="$(awk -F': ' '$1=="name" {print $2}' <<<"$config")"
        template_flag="$(awk -F': ' '$1=="template" {print $2}' <<<"$config")"
        protection_flag="$(awk -F': ' '$1=="protection" {print $2}' <<<"$config")"
        description="$(awk -F': ' '$1=="description" {sub(/^description: /, ""); print; exit}' <<<"$config")"
        ciuser="$(awk -F': ' '$1=="ciuser" {print $2}' <<<"$config")"
        if [[ "$name" != "$TEMPLATE_NAME" || "$template_flag" != "1" ]]; then
            die "VMID ${TEMPLATE_VMID} уже существует, но это не ожидаемый шаблон ${TEMPLATE_NAME}; перезапись запрещена"
        fi
        if [[ "$description" != *"template-version=${TEMPLATE_VERSION}"* || "$ciuser" != "root" ]]; then
            die "Шаблон ${TEMPLATE_VMID} ${TEMPLATE_NAME} существует, но не соответствует root-only Template-Version ${TEMPLATE_VERSION}. Автоматическая замена защищённого шаблона запрещена; выполните отдельную осознанную пересборку template 9000 по актуальному scripts/pve/create-template.sh."
        fi
        if [[ "$protection_flag" == "1" ]]; then
            ok "Защищённый шаблон ${TEMPLATE_VMID} Template-Version ${TEMPLATE_VERSION} уже существует"
        else
            printf '[ИНФО] Канонический шаблон %s версии %s найден без protection=1; защита будет включена только после снимка конфигурации.\n' "$TEMPLATE_VMID" "$TEMPLATE_VERSION"
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
    pveam list local >"$dst/diagnostics/lxc-templates-local.txt" 2>&1 || true
    pveum user list >"$dst/diagnostics/users.txt" 2>&1 || true
    pveum role list >"$dst/diagnostics/roles.txt" 2>&1 || true
    pveum acl list >"$dst/diagnostics/acl.txt" 2>&1 || true
    pveum pool list >"$dst/diagnostics/pools.txt" 2>&1 || true
    ip addr >"$dst/diagnostics/ip-addr.txt" 2>&1 || true
    ip route >"$dst/diagnostics/ip-route.txt" 2>&1 || true
    if qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        qm config "$TEMPLATE_VMID" >"$dst/diagnostics/template-${TEMPLATE_VMID}-before.txt" 2>&1 || true
    fi

    ok "Сохранён снимок конфигурации хоста: $dst"
}

# =============================================================================
# Репозитории, пакеты, сеть и storage
# =============================================================================

configure_ceph_repository() {
    local ceph_sources="/etc/apt/sources.list.d/ceph.sources"
    local legacy_ceph_list="/etc/apt/sources.list.d/ceph.list"
    local uri suite component uri_count suite_count component_count release tmp

    # PVE 9 использует deb822 ceph.sources. Активный legacy ceph.list на trixie
    # считаем нестандартной конфигурацией: автоматически смешивать/переписывать
    # разные форматы Ceph repositories небезопасно.
    if [[ -f "$legacy_ceph_list" ]] \
        && grep -Ev '^[[:space:]]*(#|$)' "$legacy_ceph_list" | grep -qi 'ceph'; then
        die "Обнаружен активный legacy Ceph repository ${legacy_ceph_list}. Для PVE 9 ожидается ceph.sources; автоматическое изменение нестандартной конфигурации запрещено."
    fi

    if [[ ! -f "$ceph_sources" ]]; then
        ok "Ceph repository не настроен; изменений не требуется"
        return
    fi

    uri_count="$(grep -Ec '^URIs:[[:space:]]*' "$ceph_sources" || true)"
    suite_count="$(grep -Ec '^Suites:[[:space:]]*' "$ceph_sources" || true)"
    component_count="$(grep -Ec '^Components:[[:space:]]*' "$ceph_sources" || true)"

    [[ "$uri_count" == "1" && "$suite_count" == "1" && "$component_count" == "1" ]] \
        || die "Файл ${ceph_sources} содержит несколько или неполные repository stanzas. Bootstrap не будет переписывать нестандартную Ceph-конфигурацию автоматически."

    # Не разбиваем поле по ':' — URI содержит https:// и должен оставаться целиком.
    uri="$(sed -n 's/^URIs:[[:space:]]*//p' "$ceph_sources" | head -n1)"
    suite="$(sed -n 's/^Suites:[[:space:]]*//p' "$ceph_sources" | head -n1)"
    component="$(sed -n 's/^Components:[[:space:]]*//p' "$ceph_sources" | head -n1)"

    if [[ "$uri" =~ ^https?://download\.proxmox\.com/debian/(ceph-[A-Za-z0-9._-]+)$ \
        && "$suite" == "trixie" && "$component" == "no-subscription" ]]; then
        ok "Ceph repository уже использует no-subscription (${BASH_REMATCH[1]})"
        return
    fi

    if [[ "$uri" =~ ^https?://enterprise\.proxmox\.com/debian/(ceph-[A-Za-z0-9._-]+)$ \
        && "$suite" == "trixie" && "$component" == "enterprise" ]]; then
        release="${BASH_REMATCH[1]}"
        tmp="$(mktemp "${ceph_sources}.tmp.XXXXXX")"

        if ! sed \
            -e "s#^URIs:[[:space:]]*https\?://enterprise\.proxmox\.com/debian/${release}[[:space:]]*\$#URIs: http://download.proxmox.com/debian/${release}#" \
            -e 's/^Components:[[:space:]]*enterprise[[:space:]]*$/Components: no-subscription/' \
            "$ceph_sources" >"$tmp"; then
            rm -f -- "$tmp"
            die "Не удалось преобразовать Ceph enterprise repository в no-subscription"
        fi

        if ! install -o root -g root -m 0644 "$tmp" "$ceph_sources"; then
            rm -f -- "$tmp"
            die "Не удалось установить обновлённую конфигурацию Ceph repository"
        fi
        rm -f -- "$tmp"

        uri="$(sed -n 's/^URIs:[[:space:]]*//p' "$ceph_sources" | head -n1)"
        component="$(sed -n 's/^Components:[[:space:]]*//p' "$ceph_sources" | head -n1)"
        [[ "$uri" == "http://download.proxmox.com/debian/${release}" \
            && "$component" == "no-subscription" ]] \
            || die "После изменения не удалось подтвердить Ceph no-subscription repository для ${release}"

        ok "Ceph ${release}: enterprise repository переведён на no-subscription без смены release"
        return
    fi

    die "Файл ${ceph_sources} имеет нестандартную Ceph repository configuration (URIs='${uri:-не задан}', Suites='${suite:-не задан}', Components='${component:-не задан}'). Bootstrap не будет переписывать её автоматически."
}

configure_pve_repository() {
    local pve_sources="/etc/apt/sources.list.d/proxmox.sources"
    local old_bootstrap_sources="/etc/apt/sources.list.d/pve-no-subscription.sources"
    local uri suite component uri_count suite_count component_count

    if [[ -f "$pve_sources" ]]; then
        uri_count="$(grep -Ec '^URIs:[[:space:]]*' "$pve_sources" || true)"
        suite_count="$(grep -Ec '^Suites:[[:space:]]*' "$pve_sources" || true)"
        component_count="$(grep -Ec '^Components:[[:space:]]*' "$pve_sources" || true)"
        [[ "$uri_count" == "1" && "$suite_count" == "1" && "$component_count" == "1" ]] \
            || die "Файл ${pve_sources} содержит несколько или неполные repository stanzas. Bootstrap не будет переписывать нестандартную PVE-конфигурацию автоматически."

        uri="$(sed -n 's/^URIs:[[:space:]]*//p' "$pve_sources" | head -n1)"
        suite="$(sed -n 's/^Suites:[[:space:]]*//p' "$pve_sources" | head -n1)"
        component="$(sed -n 's/^Components:[[:space:]]*//p' "$pve_sources" | head -n1)"
        [[ "$uri" == "http://download.proxmox.com/debian/pve" \
            && "$suite" == "trixie" && "$component" == "pve-no-subscription" ]] \
            || die "Существующий ${pve_sources} имеет неожиданную конфигурацию; автоматическая перезапись запрещена"
        ok "PVE repository уже использует pve-no-subscription"
    else
        cat >"$pve_sources" <<'EOF_PVE_REPO'
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
EOF_PVE_REPO
        chmod 0644 "$pve_sources"
        ok "Создан PVE no-subscription repository: ${pve_sources}"
    fi

    if [[ -f "$old_bootstrap_sources" ]]; then
        uri="$(sed -n 's/^URIs:[[:space:]]*//p' "$old_bootstrap_sources" | head -n1)"
        suite="$(sed -n 's/^Suites:[[:space:]]*//p' "$old_bootstrap_sources" | head -n1)"
        component="$(sed -n 's/^Components:[[:space:]]*//p' "$old_bootstrap_sources" | head -n1)"
        if [[ "$uri" == "http://download.proxmox.com/debian/pve" \
            && "$suite" == "trixie" && "$component" == "pve-no-subscription" ]]; then
            rm -f -- "$old_bootstrap_sources"
            ok "Удалён дублирующий старый bootstrap repository: ${old_bootstrap_sources}"
        else
            die "${old_bootstrap_sources} существует, но имеет неожиданное содержимое; автоматическое удаление запрещено"
        fi
    fi
}

configure_apt() {
    log "Настройка PVE/Ceph repository policy без subscription"

    install -d -m 0755 /etc/apt/sources.list.d
    configure_pve_repository

    local f
    for f in \
        /etc/apt/sources.list.d/pve-enterprise.list \
        /etc/apt/sources.list.d/pve-enterprise.sources; do
        if [[ -f "$f" ]]; then
            mv -f "$f" "${f}.disabled"
            ok "Отключён репозиторий enterprise: $f"
        fi
    done

    configure_ceph_repository

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

    for cmd in git ssh ssh-keygen python3 curl jq runuser find; do
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
    curl -fsS --connect-timeout 10 -o /dev/null \
        http://download.proxmox.com/debian/pve/dists/trixie/InRelease \
        || die "Нет доступа к PVE repository download.proxmox.com"

    ok "DNS, GitHub HTTPS и доступ к PVE repository работают"
}

storage_content_list() {
    local storage=$1
    pvesh get "/storage/${storage}" --output-format json \
        | jq -r '.content // empty'
}

storage_has_content() {
    local storage=$1 required=$2 content
    content="$(storage_content_list "$storage")"
    tr ',' '\n' <<<"$content" | grep -qx "$required"
}

ensure_storage_contents() {
    local storage=$1
    shift
    local content new_content required
    local -a missing=()

    content="$(storage_content_list "$storage")"
    [[ -n "$content" ]] || die "Не удалось определить content types storage ${storage}"

    for required in "$@"; do
        if ! tr ',' '\n' <<<"$content" | grep -qx "$required"; then
            missing+=("$required")
        fi
    done

    if (( ${#missing[@]} == 0 )); then
        ok "Storage ${storage} уже содержит обязательные content types: $*"
        return
    fi

    new_content="$content"
    for required in "${missing[@]}"; do
        new_content="${new_content},${required}"
    done

    pvesm set "$storage" --content "$new_content"

    for required in "$@"; do
        storage_has_content "$storage" "$required" \
            || die "После изменения storage ${storage} по-прежнему не поддерживает content type ${required}"
    done

    ok "В storage ${storage} добавлены content types без удаления существующих: ${missing[*]}"
}

storage_status_json() {
    local node
    node="$(hostname -s)"
    pvesh get "/nodes/${node}/storage" --output-format json
}

storage_is_active_enabled() {
    local storage=$1
    storage_status_json \
        | jq -e --arg storage "$storage" '
            .[]
            | select(.storage == $storage)
            | select((.enabled // 1) == 1 or (.enabled // 1) == true)
            | select(.active == 1 or .active == true)
        ' >/dev/null
}

ensure_storage_layout() {
    log "Проверка активности и content types базовых storage"

    storage_is_active_enabled local \
        || die "Storage local не активен или отключён на этом PVE node"
    storage_is_active_enabled local-lvm \
        || die "Storage local-lvm не активен или отключён на этом PVE node"

    ensure_storage_contents local snippets vztmpl
    ensure_storage_contents local-lvm images rootdir

    ok "Storage local/local-lvm активны и готовы для VM, LXC, templates и snippets"
}

ensure_lxc_template() {
    log "Проверка Debian 13 LXC template"

    pveam update || die "Не удалось обновить каталог Proxmox LXC templates через pveam update"

    local template
    template="$(pveam available --section system \
        | awk '$1=="system" && $2 ~ /^debian-13-standard_.*_amd64\.tar\.(zst|xz|gz)$/ {print $2}' \
        | sort -V \
        | tail -n1)"

    [[ -n "$template" ]] \
        || die "В каталоге pveam не найден Debian 13 standard LXC template"

    LXC_TEMPLATE_VOLUME="${LXC_TEMPLATE_STORAGE}:vztmpl/${template}"

    if pveam list "$LXC_TEMPLATE_STORAGE" \
        | awk 'NR>1 {print $1}' \
        | grep -Fxq "$LXC_TEMPLATE_VOLUME"; then
        ok "Актуальный Debian 13 LXC template уже загружен: ${LXC_TEMPLATE_VOLUME}"
        return
    fi

    pveam download "$LXC_TEMPLATE_STORAGE" "$template" \
        || die "Не удалось скачать Debian 13 LXC template ${template} в storage ${LXC_TEMPLATE_STORAGE}"

    pveam list "$LXC_TEMPLATE_STORAGE" \
        | awk 'NR>1 {print $1}' \
        | grep -Fxq "$LXC_TEMPLATE_VOLUME" \
        || die "pveam download завершился, но ${LXC_TEMPLATE_VOLUME} не найден в storage"

    ok "Debian 13 LXC template загружен: ${LXC_TEMPLATE_VOLUME}"
}

storage_available_bytes() {
    local storage=$1
    storage_status_json \
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

# =============================================================================
# Каноническая файловая структура и постоянные host-side credentials
# =============================================================================

validate_config_yaml() {
    local file="$CONFIG_DIR/config.yaml"

    python3 - "$file" \
        "zsergeyru/proxmox" "$PRIVATE_BRANCH" "$REPO_DIR" "$MANAGED_POOL" \
        "$HOST_PVE_TOKEN" "$AI_PVE_TOKEN" "$TEMPLATE_VMID" <<'PY_CONFIG'
import sys
import yaml

path = sys.argv[1]
expected = {
    "repo": sys.argv[2],
    "branch": sys.argv[3],
    "checkout": sys.argv[4],
    "managed_pool": sys.argv[5],
    "host_deploy_identity": sys.argv[6],
    "ai_infra_identity": sys.argv[7],
    "template_vmid": sys.argv[8],
}

try:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
except Exception as exc:
    print(f"Не удалось прочитать {path}: {exc}", file=sys.stderr)
    raise SystemExit(1)

if not isinstance(data, dict):
    print(f"{path} должен содержать YAML mapping", file=sys.stderr)
    raise SystemExit(1)

errors = []
for key, expected_value in expected.items():
    if key not in data:
        errors.append(f"отсутствует ключ {key}")
        continue
    actual = data[key]
    if str(actual) != str(expected_value):
        errors.append(f"{key}: ожидается {expected_value!r}, обнаружено {actual!r}")

if errors:
    print(f"{path} не соответствует текущей bootstrap-конфигурации:", file=sys.stderr)
    for item in errors:
        print(f"  - {item}", file=sys.stderr)
    raise SystemExit(1)
PY_CONFIG
}

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

    validate_config_yaml \
        || die "Существующий ${CONFIG_DIR}/config.yaml конфликтует с текущей моделью bootstrap. Автоматическая перезапись запрещена."
    ok "${CONFIG_DIR}/config.yaml соответствует текущей bootstrap-конфигурации"
}

ensure_pve_guest_key() {
    log "Проверка постоянной PVE guest SSH identity"

    if [[ ! -e "$PVE_GUEST_KEY" && ! -e "$PVE_GUEST_PUB" ]]; then
        if [[ -f "$LEGACY_GUEST_BOOTSTRAP_KEY" ]]; then
            install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0600 \
                "$LEGACY_GUEST_BOOTSTRAP_KEY" "$PVE_GUEST_KEY"
            rm -f -- "$LEGACY_GUEST_BOOTSTRAP_KEY" "$LEGACY_GUEST_BOOTSTRAP_PUB"
            ok "Legacy PVE guest SSH private key перенесён в canonical path без ротации"
        elif [[ -e "$LEGACY_GUEST_BOOTSTRAP_PUB" ]]; then
            die "Обнаружен legacy public key ${LEGACY_GUEST_BOOTSTRAP_PUB}, но legacy private key отсутствует. Автоматическая ротация запрещена: восстановите private key из backup либо выполните отдельную осознанную rotation operation."
        fi
    fi

    if [[ -f "$PVE_GUEST_KEY" && -f "$LEGACY_GUEST_BOOTSTRAP_KEY" ]]; then
        local canonical_pub legacy_pub
        canonical_pub="$(ssh-keygen -y -f "$PVE_GUEST_KEY" 2>/dev/null)" \
            || die "Не удалось прочитать canonical PVE guest private key ${PVE_GUEST_KEY}"
        legacy_pub="$(ssh-keygen -y -f "$LEGACY_GUEST_BOOTSTRAP_KEY" 2>/dev/null)" \
            || die "Не удалось прочитать legacy PVE guest private key ${LEGACY_GUEST_BOOTSTRAP_KEY}"
        [[ "$canonical_pub" == "$legacy_pub" ]] \
            || die "Одновременно существуют разные canonical и legacy PVE guest SSH identities. Автоматический выбор запрещён."
        rm -f -- "$LEGACY_GUEST_BOOTSTRAP_KEY" "$LEGACY_GUEST_BOOTSTRAP_PUB"
        ok "Удалена совпадающая legacy-копия PVE guest SSH identity"
    fi

    if [[ ! -f "$PVE_GUEST_KEY" ]]; then
        [[ ! -e "$PVE_GUEST_PUB" ]] \
            || die "Private key ${PVE_GUEST_KEY} отсутствует, но ${PVE_GUEST_PUB} существует. Возможна потеря PVE guest credential; автоматическая ротация запрещена. Восстановите private key из backup либо выполните отдельную осознанную rotation operation."

        local old_umask
        old_umask="$(umask)"
        umask 077
        if ! ssh-keygen -q -t ed25519 -N '' -C 'pve-guest' -f "$PVE_GUEST_KEY"; then
            umask "$old_umask"
            die "Не удалось создать PVE guest SSH keypair"
        fi
        umask "$old_umask"
        ok "Создан новый PVE guest SSH private key"
    else
        ok "PVE guest SSH private key уже существует и не ротируется"
    fi

    local derived_pub tmp_pub
    derived_pub="$(ssh-keygen -y -f "$PVE_GUEST_KEY" 2>/dev/null)" \
        || die "Не удалось прочитать PVE guest private key ${PVE_GUEST_KEY}"
    [[ "$derived_pub" == ssh-ed25519\ * ]] \
        || die "PVE guest key ${PVE_GUEST_KEY} должен быть Ed25519"

    chown "$DEPLOY_USER:$DEPLOY_USER" "$PVE_GUEST_KEY"
    chmod 0600 "$PVE_GUEST_KEY"

    tmp_pub="$(mktemp "${SSH_DIR}/.pve-guest-pub.XXXXXX")"
    printf '%s %s\n' "$derived_pub" 'pve-guest' >"$tmp_pub"
    install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0644 "$tmp_pub" "$PVE_GUEST_PUB"
    rm -f "$tmp_pub"

    [[ -s "$PVE_GUEST_PUB" ]] || die "PVE guest public key не создан: ${PVE_GUEST_PUB}"
    ok "PVE guest SSH identity проверена: ${PVE_GUEST_KEY}"
}

refresh_canonical_public_key() {
    local derived_pub tmp_pub
    derived_pub="$(ssh-keygen -y -f "$KEY_FILE" 2>/dev/null)" \
        || die "Не удалось прочитать canonical Deploy Key ${KEY_FILE}"
    [[ -n "$derived_pub" ]] || die "Из canonical Deploy Key не удалось получить public key"

    tmp_pub="$(mktemp "${SSH_DIR}/.github-key-pub.XXXXXX")"
    printf '%s %s\n' "$derived_pub" 'pve-canonical-readonly-zsergeyru-proxmox' >"$tmp_pub"
    install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0644 "$tmp_pub" "${KEY_FILE}.pub"
    rm -f "$tmp_pub"
}

prepare_canonical_github_access() {
    log "Принятие read-only Deploy Key из публичной Stage 0"

    if [[ ! -f "$KEY_FILE" ]]; then
        [[ -f "$STAGE0_KEY_FILE" ]] \
            || die "Канонический GitHub Deploy Key отсутствует и Stage 0 не передала временный private key. Сначала запустите публичный proxmox-bootstrap/init-pve.sh."

        install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0600 "$STAGE0_KEY_FILE" "$KEY_FILE"
        ok "Deploy Key перенесён в каноническое хранилище"
    else
        chown "$DEPLOY_USER:$DEPLOY_USER" "$KEY_FILE"
        chmod 0600 "$KEY_FILE"
        ok "Канонический GitHub Deploy Key уже существует"
    fi

    refresh_canonical_public_key

    if [[ -f "$STAGE0_KEY_FILE" ]]; then
        local canonical_pub stage0_pub
        canonical_pub="$(ssh-keygen -y -f "$KEY_FILE" 2>/dev/null || true)"
        stage0_pub="$(ssh-keygen -y -f "$STAGE0_KEY_FILE" 2>/dev/null || true)"
        [[ -n "$stage0_pub" ]] || die "Stage 0 передала нечитаемый private Deploy Key: ${STAGE0_KEY_FILE}"
        if [[ "$canonical_pub" != "$stage0_pub" ]]; then
            CANONICAL_KEY_DIFFERS_FROM_STAGE0=1
            warn "Stage 0 использует другой Deploy Key, чем уже существующий canonical key. Bootstrap не заменяет постоянный credential автоматически."
        fi
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
    local refs
    if ! refs="$(git_as_deployer ls-remote "$PRIVATE_REPO" "refs/heads/${PRIVATE_BRANCH}" 2>/dev/null)"; then
        if (( CANONICAL_KEY_DIFFERS_FROM_STAGE0 )); then
            die "Canonical Deploy Key ${KEY_FILE} не даёт доступ к ${PRIVATE_REPO}, при этом Stage 0 использует другой ключ. Автоматическая замена постоянного ключа запрещена. Проверьте ${KEY_FILE}.pub, Deploy keys GitHub и явно выполните recovery/rotation canonical credential."
        fi
        die "Канонический Deploy Key не даёт read-only доступ к ${PRIVATE_REPO}"
    fi
    [[ -n "$refs" ]] \
        || die "Канонический Deploy Key работает, но в ${PRIVATE_REPO} отсутствует ожидаемая ветка ${PRIVATE_BRANCH}"
    ok "Read-only доступ к приватному GitHub-репозиторию и ветке ${PRIVATE_BRANCH} подтверждён"
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
    local role_json actual_raw expected actual missing extra missing_raw missing_after dangerous_extra

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
        dangerous_extra="$(printf '%s\n' "$extra" | grep -E '^(Permissions\.Modify|Sys\.Modify)$' || true)"
        if [[ -n "$dangerous_extra" ]]; then
            warn "Роль ${role} содержит опасные дополнительные privileges, которые bootstrap по принятой additive policy не удаляет автоматически: $(join_lines "$dangerous_extra")"
        fi
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

ensure_host_identity_acls() {
    local userid=$1 tokenid=$2 principal_type principal

    # Host-side deployer не ограничивается pool managed: он должен уметь
    # разворачивать и обслуживать все VM/LXC, включая специальные объекты вне pool.
    # Для privsep=1 одинаковые ACL назначаются backing user и самому API-токену.
    for principal_type in user token; do
        if [[ "$principal_type" == "user" ]]; then principal="$userid"; else principal="$tokenid"; fi

        ensure_acl_entry "/vms" "$principal_type" "$principal" "$ROLE_GUEST"
        # Pool.Allocate нужен deployer для размещения обычных guests в managed.
        ensure_acl_entry "/pool/${MANAGED_POOL}" "$principal_type" "$principal" "$ROLE_POOL"
        ensure_acl_entry "/storage/local" "$principal_type" "$principal" "$ROLE_STORAGE"
        ensure_acl_entry "/storage/local-lvm" "$principal_type" "$principal" "$ROLE_STORAGE"
        ensure_acl_entry "/sdn/zones/localnetwork/${BRIDGE}" "$principal_type" "$principal" "$ROLE_NETWORK"
    done
}

ensure_ai_identity_acls() {
    local userid=$1 tokenid=$2 principal_type principal

    # AI automation ограничена pool managed. Для privsep=1 одинаковые ACL нужны
    # backing user и самому API-токену. Существующий privsep=0 не меняется.
    for principal_type in user token; do
        if [[ "$principal_type" == "user" ]]; then principal="$userid"; else principal="$tokenid"; fi

        ensure_acl_entry "/pool/${MANAGED_POOL}" "$principal_type" "$principal" "$ROLE_GUEST"
        ensure_acl_entry "/pool/${MANAGED_POOL}" "$principal_type" "$principal" "$ROLE_POOL"
        # Template 9000 доступен AI отдельно только как источник clone.
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
    local full_token=$1 scope=$2 permissions_json

    permissions_json="$(pveum user permissions "$full_token" --output-format json)" \
        || die "Не удалось получить effective permissions для ${full_token}"

    case "$scope" in
        host)
            verify_permission_set "$full_token" "$permissions_json" "/vms" \
                "$ROLE_GUEST_PRIVS"
            verify_permission_set "$full_token" "$permissions_json" "/pool/${MANAGED_POOL}" \
                "$ROLE_POOL_PRIVS"
            ;;
        ai)
            verify_permission_set "$full_token" "$permissions_json" "/pool/${MANAGED_POOL}" \
                "${ROLE_GUEST_PRIVS} ${ROLE_POOL_PRIVS}"
            verify_permission_set "$full_token" "$permissions_json" "/vms/${TEMPLATE_VMID}" \
                "$ROLE_CLONE_PRIVS"
            ;;
        *)
            die "Неизвестная модель effective permissions '${scope}' для ${full_token}"
            ;;
    esac

    verify_permission_set "$full_token" "$permissions_json" "/storage/local" \
        "$ROLE_STORAGE_PRIVS"
    verify_permission_set "$full_token" "$permissions_json" "/storage/local-lvm" \
        "$ROLE_STORAGE_PRIVS"
    verify_permission_set "$full_token" "$permissions_json" "/sdn/zones/localnetwork/${BRIDGE}" \
        "$ROLE_NETWORK_PRIVS"

    ok "Effective permissions API-токена ${full_token} соответствуют модели ${scope}"
}

verify_token_api_auth() {
    local full_token=$1 secret_file=$2
    local stored_id secret old_umask

    stored_id="$(sed -n 's/^token_id=//p' "$secret_file" | head -n1)"
    secret="$(sed -n 's/^token_secret=//p' "$secret_file" | head -n1)"

    [[ "$stored_id" == "$full_token" ]] \
        || die "В ${secret_file} записан token_id '${stored_id:-не задан}', ожидается '${full_token}'"
    [[ -n "$secret" ]] || die "В ${secret_file} отсутствует token_secret"

    # Если предыдущий процесс был убит SIGKILL, trap выполнить невозможно.
    # Перед новой проверкой удаляем только наши закрытые временные API-header files.
    find "$SECRETS_DIR" -maxdepth 1 -type f -name '.api-check.*' -delete \
        || die "Не удалось удалить оставшиеся временные файлы проверки API credential"

    old_umask="$(umask)"
    umask 077
    API_HEADER_FILE="$(mktemp "${SECRETS_DIR}/.api-check.XXXXXX")"
    umask "$old_umask"
    printf 'Authorization: PVEAPIToken=%s=%s\n' "$full_token" "$secret" >"$API_HEADER_FILE"
    chmod 0600 "$API_HEADER_FILE"

    if ! curl --fail --silent --show-error --insecure \
        --connect-timeout 10 --max-time 20 \
        --header "@${API_HEADER_FILE}" \
        https://127.0.0.1:8006/api2/json/version \
        | jq -e '(.data.version // .data.release // empty) != ""' >/dev/null; then
        cleanup_api_header_file
        unset secret
        die "API-токен ${full_token} не прошёл локальную проверку авторизации. Возможен устаревший secret или другая проблема credential."
    fi

    cleanup_api_header_file
    unset secret
    ok "API-токен ${full_token} успешно авторизуется в локальном Proxmox API"
}

verify_pve_identity() {
    local full_token=$1 secret_file=$2 scope=$3
    verify_effective_permissions "$full_token" "$scope"
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

    ensure_host_identity_acls "$HOST_PVE_USER" "$HOST_PVE_TOKEN"
    ensure_ai_identity_acls "$AI_PVE_USER" "$AI_PVE_TOKEN"

    verify_pve_identity "$HOST_PVE_TOKEN" "$HOST_TOKEN_FILE" host
    verify_pve_identity "$AI_PVE_TOKEN" "$AI_TOKEN_FILE" ai
}

# =============================================================================
# Template и локальные команды
# =============================================================================

ensure_template() {
    if pct config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        die "VMID ${TEMPLATE_VMID} занят LXC-контейнером; создание шаблона невозможно"
    fi

    if qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        local config name template_flag protection_flag description ciuser
        config="$(qm config "$TEMPLATE_VMID")"
        name="$(awk -F': ' '$1=="name" {print $2}' <<<"$config")"
        template_flag="$(awk -F': ' '$1=="template" {print $2}' <<<"$config")"
        protection_flag="$(awk -F': ' '$1=="protection" {print $2}' <<<"$config")"
        description="$(awk -F': ' '$1=="description" {sub(/^description: /, ""); print; exit}' <<<"$config")"
        ciuser="$(awk -F': ' '$1=="ciuser" {print $2}' <<<"$config")"
        [[ "$name" == "$TEMPLATE_NAME" && "$template_flag" == "1" ]] \
            || die "VMID ${TEMPLATE_VMID} существует, но не соответствует шаблону ${TEMPLATE_NAME}"
        [[ "$description" == *"template-version=${TEMPLATE_VERSION}"* && "$ciuser" == "root" ]] \
            || die "Шаблон ${TEMPLATE_VMID} не соответствует root-only Template-Version ${TEMPLATE_VERSION}; автоматическая миграция существующего template запрещена"
        if [[ "$protection_flag" != "1" ]]; then
            qm set "$TEMPLATE_VMID" --protection 1
            protection_flag="$(qm config "$TEMPLATE_VMID" | awk -F': ' '$1=="protection" {print $2}')"
            [[ "$protection_flag" == "1" ]] \
                || die "Не удалось установить protection=1 для шаблона ${TEMPLATE_VMID} ${TEMPLATE_NAME}"
            ok "Для существующего шаблона ${TEMPLATE_VMID} автоматически включён protection=1"
        else
            ok "Шаблон ${TEMPLATE_VMID} ${TEMPLATE_NAME} Template-Version ${TEMPLATE_VERSION} уже существует и защищён"
        fi
        return
    fi

    local builder="${REPO_DIR}/scripts/pve/create-template.sh"
    [[ -f "$builder" ]] || die "Не найден канонический скрипт создания шаблона: $builder"

    log "Создание защищённого Debian-шаблона ${TEMPLATE_VMID} Template-Version ${TEMPLATE_VERSION}"
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
    grep -q '^ciuser: root$' <<<"$final_config" \
        || die "VMID ${TEMPLATE_VMID} создан, но ciuser не root"
    grep -q "template-version=${TEMPLATE_VERSION}" <<<"$final_config" \
        || die "VMID ${TEMPLATE_VMID} создан, но description не содержит ожидаемую Template-Version ${TEMPLATE_VERSION}"

    ok "Создан защищённый шаблон ${TEMPLATE_VMID} ${TEMPLATE_NAME} Template-Version ${TEMPLATE_VERSION}"
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
        if [[ -x /usr/local/sbin/deploy-guest ]]; then
            warn "Исходный файл deploy-guest отсутствует, а старая wrapper-команда /usr/local/sbin/deploy-guest всё ещё существует. Она не считается готовым компонентом."
        else
            warn "Исходный файл deploy-guest пока отсутствует: ${deployer_src}"
        fi
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
    local deployer_src="${REPO_DIR}/scripts/pve/deploy-guest.py"

    printf '\nСОСТОЯНИЕ ПРИВАТНОЙ ИНИЦИАЛИЗАЦИИ PVE\n\n'
    printf '[ОК] Proxmox VE 9 / Debian trixie / KVM проверены\n'
    printf '[ОК] Эксклюзивная блокировка bootstrap используется\n'
    printf '[ОК] PVE/Ceph repository policy без subscription проверена\n'
    printf '[ОК] DNS, GitHub HTTPS и доступ к PVE repository работают\n'
    printf '[ОК] local/local-lvm готовы для images/rootdir/vztmpl/snippets\n'
    printf '[ОК] Debian 13 LXC template подготовлен: %s\n' "${LXC_TEMPLATE_VOLUME:-не определён}"
    printf '[ОК] pvedeploy, config.yaml и файловая структура проверены\n'
    printf '[ОК] PVE guest SSH identity создана/проверена\n'
    printf '[ОК] Канонический read-only Deploy Key установлен\n'
    printf '[ОК] Приватный репозиторий синхронизирован и origin проверен\n'
    printf '[ОК] managed и роли Proxmox проверены\n'
    printf '[ОК] %s: guest-level /vms, effective permissions и API credential проверены\n' "$HOST_PVE_TOKEN"
    printf '[ОК] %s: managed-only effective permissions и API credential проверены\n' "$AI_PVE_TOKEN"

    if qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        printf '[ОК] Шаблон %s Template-Version %s существует и protection проверен\n' "$TEMPLATE_VMID" "$TEMPLATE_VERSION"
    else
        printf '[НЕТ] Шаблон %s отсутствует\n' "$TEMPLATE_VMID"
        ready=0
    fi

    if [[ -x /usr/local/sbin/deploy-guest && -f "$deployer_src" ]]; then
        printf '[ОК] Команда deploy-guest установлена и source существует\n'
    elif [[ -x /usr/local/sbin/deploy-guest ]]; then
        printf '[ОЖИДАНИЕ] Обнаружена старая deploy-guest wrapper, но её source отсутствует\n'
        ready=0
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
    ensure_storage_layout
    ensure_lxc_template
    ensure_runtime_layout
    ensure_pve_guest_key
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
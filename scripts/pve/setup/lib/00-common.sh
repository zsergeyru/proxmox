#!/usr/bin/env bash

PVE_CONFIGURATION_VERSION=29

PRIVATE_REPO="git@github.com:zsergeyru/proxmox.git"
PRIVATE_BRANCH="main"
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
LOCK_FILE="/run/lock/proxmox-orchestration.lock"
LOG_DIR="/var/log/proxmox-deployer"
CONFIGURATION_LOG_DIR="$LOG_DIR"
CONFIGURATION_LOG_FILE="${CONFIGURATION_LOG_DIR}/configure-pve.log"
BACKUP_ROOT="/var/backups/proxmox-configuration"
SECRETS_BACKUP_ROOT="/var/backups/proxmox-secrets"

KEY_FILE="${SSH_DIR}/github_proxmox_repo_ed25519"
SSH_CONFIG="${SSH_DIR}/config"
KNOWN_HOSTS="${SSH_DIR}/known_hosts"
PVE_GUEST_KEY="${SSH_DIR}/pve_guest_ed25519"
PVE_GUEST_PUB="${PVE_GUEST_KEY}.pub"
PVE_CA_SOURCE="/etc/pve/pve-root-ca.pem"
PVE_CA_FILE="${CONFIG_DIR}/pve-root-ca.pem"

TEMPLATE_VMID=9000
TEMPLATE_NAME="tpl-debian13"
TEMPLATE_VERSION=7
TEMPLATE_BUILDER_NAME="builder-debian13"
TEMPLATE_DISK_STORAGE="local-lvm"
TEMPLATE_SNIPPET_STORAGE="local"
TEMPLATE_MEMORY_MB=1024
TEMPLATE_CORES=1
TEMPLATE_DISK_SIZE="16G"
TEMPLATE_WAIT_SECONDS=1200
TEMPLATE_WAIT_PROGRESS_SECONDS=15
TEMPLATE_IMAGE_URL="https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2"
TEMPLATE_CHECKSUM_URL="https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS"
TEMPLATE_IMAGE_NAME="${TEMPLATE_IMAGE_URL##*/}"
TEMPLATE_IMAGE_DIR="/var/lib/vz/template/cache/debian13"
TEMPLATE_IMAGE_PATH="${TEMPLATE_IMAGE_DIR}/${TEMPLATE_IMAGE_NAME}"
TEMPLATE_CHECKSUM_PATH="${TEMPLATE_IMAGE_DIR}/SHA512SUMS"
TEMPLATE_SNIPPET_NAME="debian13-template-builder-${TEMPLATE_VMID}.yaml"
TEMPLATE_SNIPPET_VOL="${TEMPLATE_SNIPPET_STORAGE}:snippets/${TEMPLATE_SNIPPET_NAME}"
TEMPLATE_ASSET_DIR="${REPO_DIR}/templates/debian13"
TEMPLATE_RENDERER="${REPO_DIR}/scripts/pve/setup/render-template-cloud-init.py"
TEMPLATE_MIN_LOCAL_BYTES=$((2 * 1024 * 1024 * 1024))
TEMPLATE_MIN_DISK_STORAGE_BYTES=$((18 * 1024 * 1024 * 1024))

# Штатный Full Clone smoke-test базового VM template.
SMOKE_VMID=9099
SMOKE_NAME="smoke-debian13-9099"
SMOKE_DISK_SIZE="20G"
SMOKE_MIN_ROOT_BYTES=$((18 * 1024 * 1024 * 1024))
TEMPLATE_SMOKE_STATE_FILE="${STATE_DIR}/template-smoke.json"

MANAGED_POOL="managed"
BRIDGE="vmbr0"
LXC_TEMPLATE_STORAGE="local"

UPDATE_SYSTEM=0
SMOKE_TEST_TEMPLATE=0
WARN_COUNT=0
RUN_SOURCE_REVISION="${PVE_CONFIGURATION_SOURCE_REVISION:-}"
REPO_REVISION=""
CONFIGURATION_RUNNING=0
API_HEADER_FILE=""
TEMPLATE_DOWNLOAD_TMP=""
SMOKE_SSH_KNOWN_HOSTS=""
LXC_TEMPLATE_VOLUME=""
TEMPLATE_BUILD_REQUIRED=0
TEMPLATE_NEEDS_PROTECTION=0
TEMPLATE_BUILD_ACTIVE=0
TEMPLATE_BUILT_THIS_RUN=0
SMOKE_ACTIVE=0
TEMPLATE_IMAGE_SHA512=""
TEMPLATE_SNIPPET_PATH=""
TEMPLATE_KERNEL_VERSION=""
TEMPLATE_FRAMEBUFFER_SIZE=""
PENDING_TOKEN_USER=""
PENDING_TOKEN_NAME=""
PENDING_TOKEN_FULL=""

HOST_PVE_USER="deployer@pve"
HOST_PVE_TOKEN_NAME="host-deploy"
HOST_PVE_TOKEN="${HOST_PVE_USER}!${HOST_PVE_TOKEN_NAME}"
HOST_TOKEN_FILE="${SECRETS_DIR}/host-deploy.token"

AI_PVE_USER="ai-agent@pve"
AI_PVE_TOKEN_NAME="infra"
AI_PVE_TOKEN="${AI_PVE_USER}!${AI_PVE_TOKEN_NAME}"
AI_TOKEN_FILE="${SECRETS_DIR}/ai-agent-infra.token"

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

COLOR_ENABLED=0
if [[ -t 1 && -z "${NO_COLOR:-}" && "${TERM:-dumb}" != "dumb" ]]; then
    COLOR_ENABLED=1
fi

if (( COLOR_ENABLED )); then
    C_RESET=$'\033[0m'
    C_BOLD=$'\033[1m'
    C_BLUE=$'\033[34m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_RED=$'\033[31m'
    C_CYAN=$'\033[36m'
    C_MAGENTA=$'\033[35m'
else
    C_RESET="" C_BOLD="" C_BLUE="" C_GREEN="" C_YELLOW="" C_RED="" C_CYAN="" C_MAGENTA=""
fi

log()  { printf '\n%s%s==> %s%s\n' "$C_BOLD" "$C_BLUE" "$*" "$C_RESET"; }
ok()   { printf '%s%s[ОК]%s %s\n' "$C_BOLD" "$C_GREEN" "$C_RESET" "$*"; }
info() { printf '%s%s[ИНФО]%s %s\n' "$C_BOLD" "$C_CYAN" "$C_RESET" "$*"; }
warn() {
    WARN_COUNT=$((WARN_COUNT + 1))
    printf '%s%s[ПРЕДУПРЕЖДЕНИЕ]%s %s\n' "$C_BOLD" "$C_YELLOW" "$C_RESET" "$*" >&2
}

state_revision() {
    if [[ -n "$REPO_REVISION" ]]; then
        printf '%s\n' "$REPO_REVISION"
    else
        printf '%s\n' "$RUN_SOURCE_REVISION"
    fi
}

template_build_diagnostic_hint() {
    if (( TEMPLATE_BUILD_ACTIVE )); then
        printf '%s%s[ДИАГНОСТИКА]%s VM-сборщик %s и её диски оставлены без автоматического удаления.\n' \
            "$C_BOLD" "$C_YELLOW" "$C_RESET" "$TEMPLATE_VMID" >&2
        printf '             Проверьте VM %s, Cloud-Init/QGA и snippet %s перед осознанной очисткой.\n' \
            "$TEMPLATE_VMID" "$TEMPLATE_SNIPPET_VOL" >&2
    fi
}

smoke_test_diagnostic_hint() {
    if (( SMOKE_ACTIVE )); then
        printf '%s%s[ДИАГНОСТИКА]%s Full Clone smoke VM %s намеренно оставлена для разбора ошибки.\n' \
            "$C_BOLD" "$C_YELLOW" "$C_RESET" "$SMOKE_VMID" >&2
        printf '             Не удаляйте её автоматически: проверьте qm config/status, QGA, Cloud-Init и SSH.\n' >&2
    fi
}

die() {
    local message=$*
    printf '\n%s%sОШИБКА:%s %s\n' "$C_BOLD" "$C_RED" "$C_RESET" "$message" >&2
    template_build_diagnostic_hint
    smoke_test_diagnostic_hint
    if (( CONFIGURATION_RUNNING )); then
        trap - ERR
        write_state "failed" "$(state_revision)" >/dev/null 2>&1 || true
    fi
    exit 1
}

usage() {
    cat <<'USAGE'
Использование:
  configure-pve.sh [--update-system] [--smoke-test-template] [--help]

PVE Configuration — повторяемое приведение Proxmox VE к конфигурации проекта.
Обычно запускается автоматически через публичный bootstrap-pve.sh после получения
или обновления приватного репозитория zsergeyru/proxmox.

Параметры:
  --update-system        дополнительно выполнить apt full-upgrade Proxmox/Debian
  --smoke-test-template  выполнить Full Clone smoke-test существующего template 9000 через VMID 9099
  -h, --help             показать эту справку
USAGE
}

parse_configuration_args() {
    while (($#)); do
        case "$1" in
            --update-system) UPDATE_SYSTEM=1 ;;
            --smoke-test-template) SMOKE_TEST_TEMPLATE=1 ;;
            -h|--help) usage; exit 0 ;;
            *) die "Неизвестный параметр: $1" ;;
        esac
        shift
    done
}

cleanup_api_header_file() {
    if [[ -n "${API_HEADER_FILE:-}" ]]; then
        rm -f -- "$API_HEADER_FILE" 2>/dev/null || true
        API_HEADER_FILE=""
    fi
}

cleanup_template_download_tmp() {
    if [[ -n "${TEMPLATE_DOWNLOAD_TMP:-}" ]]; then
        rm -f -- "$TEMPLATE_DOWNLOAD_TMP" 2>/dev/null || true
        TEMPLATE_DOWNLOAD_TMP=""
    fi
}

cleanup_smoke_known_hosts() {
    if [[ -n "${SMOKE_SSH_KNOWN_HOSTS:-}" ]]; then
        rm -f -- "$SMOKE_SSH_KNOWN_HOSTS" 2>/dev/null || true
        SMOKE_SSH_KNOWN_HOSTS=""
    fi
}

cleanup_pending_token() {
    [[ -n "${PENDING_TOKEN_USER:-}" && -n "${PENDING_TOKEN_NAME:-}" ]] || return 0

    if command -v pveum >/dev/null 2>&1 \
        && pveum user token delete "$PENDING_TOKEN_USER" "$PENDING_TOKEN_NAME" >/dev/null 2>&1; then
        printf '%s%s[ОТКАТ]%s Удалён незавершённый API-токен %s; его одноразовый secret не был подтверждён на диске.\n' \
            "$C_BOLD" "$C_YELLOW" "$C_RESET" "${PENDING_TOKEN_FULL:-${PENDING_TOKEN_USER}!${PENDING_TOKEN_NAME}}" >&2
    else
        printf '%s%s[ОШИБКА ОТКАТА]%s Не удалось удалить pending API-токен %s. Перед повторным запуском проверьте/ротируйте этот token вручную.\n' \
            "$C_BOLD" "$C_RED" "$C_RESET" "${PENDING_TOKEN_FULL:-${PENDING_TOKEN_USER}!${PENDING_TOKEN_NAME}}" >&2
    fi

    PENDING_TOKEN_USER=""
    PENDING_TOKEN_NAME=""
    PENDING_TOKEN_FULL=""
}

cleanup_ephemeral_files() {
    cleanup_api_header_file
    cleanup_template_download_tmp
    cleanup_smoke_known_hosts
    cleanup_pending_token
}

on_error() {
    local rc=$?
    trap - ERR
    if (( CONFIGURATION_RUNNING )); then
        write_state "failed" "$(state_revision)" >/dev/null 2>&1 || true
    fi
    printf '\n%s%sPVE Configuration аварийно остановлена.%s Код возврата: %s.\n' "$C_BOLD" "$C_RED" "$C_RESET" "$rc" >&2
    template_build_diagnostic_hint
    smoke_test_diagnostic_hint
    exit "$rc"
}

on_signal() {
    local rc=$1 signal_name=$2
    trap - ERR INT TERM
    cleanup_ephemeral_files
    if (( CONFIGURATION_RUNNING )); then
        write_state "interrupted" "$(state_revision)" >/dev/null 2>&1 || true
    fi
    printf '\n%s%sPVE Configuration прервана сигналом %s.%s\n' "$C_BOLD" "$C_RED" "$signal_name" "$C_RESET" >&2
    template_build_diagnostic_hint
    smoke_test_diagnostic_hint
    exit "$rc"
}

install_configuration_traps() {
    trap on_error ERR
    trap cleanup_ephemeral_files EXIT
    trap 'on_signal 130 INT' INT
    trap 'on_signal 143 TERM' TERM
}

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || die "Не найдена обязательная команда: $1"
}

setup_configuration_log() {
    mkdir -p "$CONFIGURATION_LOG_DIR"
    chmod 0750 "$CONFIGURATION_LOG_DIR"
    touch "$CONFIGURATION_LOG_FILE"
    chmod 0600 "$CONFIGURATION_LOG_FILE"

    # Терминал остаётся цветным, а файл получает тот же поток без ANSI-кодов.
    if (( COLOR_ENABLED )); then
        exec > >(tee >(sed -u $'s/\033\\[[0-9;]*m//g' >>"$CONFIGURATION_LOG_FILE")) 2>&1
    else
        exec > >(tee -a "$CONFIGURATION_LOG_FILE") 2>&1
    fi
}

acquire_configuration_lock() {
    require_cmd flock
    install -d -m 0755 /run/lock

    if [[ "${PVE_ORCHESTRATION_LOCK_HELD:-0}" == "1" ]]; then
        local inherited_target
        [[ -e /proc/$$/fd/9 ]] \
            || die "Public Bootstrap сообщил об унаследованной orchestration lock, но fd 9 отсутствует"
        inherited_target="$(readlink "/proc/$$/fd/9" 2>/dev/null || true)"
        [[ "$inherited_target" == "$LOCK_FILE" ]] \
            || die "Унаследованный fd 9 указывает на '${inherited_target:-неизвестно}', ожидается ${LOCK_FILE}"
        ok "Используется orchestration lock, унаследованная от Public Bootstrap"
        return
    fi

    exec 9>"$LOCK_FILE"
    flock -n 9 \
        || die "Другой Public Bootstrap или PVE Configuration уже выполняется. Параллельное изменение canonical runtime запрещено."
    ok "Получена эксклюзивная orchestration lock PVE Configuration"
}

write_state() {
    local status=$1
    local revision=${2:-$(state_revision)}
    local now state_tmp last_tmp version_tmp
    now="$(date --iso-8601=seconds)"

    mkdir -p "$STATE_DIR"
    chmod 0750 "$STATE_DIR"

    state_tmp="$(mktemp "${STATE_DIR}/.state.json.tmp.XXXXXX")"
    last_tmp="$(mktemp "${STATE_DIR}/.last-run.json.tmp.XXXXXX")"
    version_tmp="$(mktemp "${STATE_DIR}/.version.tmp.XXXXXX")"

    cat >"$state_tmp" <<EOF_STATE
{
  "configuration_version": ${PVE_CONFIGURATION_VERSION},
  "component": "pve-configuration",
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

    cat >"$last_tmp" <<EOF_LAST
{
  "timestamp": "${now}",
  "component": "pve-configuration",
  "result": "${status}",
  "repository_revision": "${revision}",
  "warnings": ${WARN_COUNT}
}
EOF_LAST

    printf '%s\n' "$PVE_CONFIGURATION_VERSION" >"$version_tmp"
    chmod 0644 "$state_tmp" "$last_tmp" "$version_tmp"
    mv -f "$state_tmp" "$STATE_FILE"
    mv -f "$last_tmp" "$LAST_RUN_FILE"
    mv -f "$version_tmp" "$VERSION_FILE"
}

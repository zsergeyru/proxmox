#!/usr/bin/env bash

PVE_CONFIGURATION_VERSION=13

PRIVATE_REPO="git@github.com:zsergeyru/proxmox.git"
PRIVATE_BRANCH="main"
DEPLOY_USER="pvedeploy"

# Временный runtime Public Bootstrap. Новые имена переменных канонические;
# PVE_STAGE0_* читаются только для совместимости с незавершённым старым bootstrap.
BOOTSTRAP_TEMP_DIR="${PVE_BOOTSTRAP_TEMP_DIR:-${PVE_STAGE0_DIR:-/var/lib/proxmox-bootstrap}}"
BOOTSTRAP_KEY_FILE="${PVE_BOOTSTRAP_KEY_FILE:-${PVE_STAGE0_KEY_FILE:-${BOOTSTRAP_TEMP_DIR}/github_proxmox_repo_ed25519}}"
BOOTSTRAP_KEY_PUB_FILE="${BOOTSTRAP_KEY_FILE}.pub"
BOOTSTRAP_KNOWN_HOSTS="${PVE_BOOTSTRAP_KNOWN_HOSTS:-${PVE_STAGE0_KNOWN_HOSTS:-${BOOTSTRAP_TEMP_DIR}/known_hosts}}"

CONFIG_DIR="/etc/proxmox-deployer"
SSH_DIR="${CONFIG_DIR}/ssh"
SECRETS_DIR="${CONFIG_DIR}/secrets"
RUNTIME_DIR="/var/lib/proxmox-deployer"
REPO_DIR="${RUNTIME_DIR}/repo"
STATE_DIR="${RUNTIME_DIR}/state"
STATE_FILE="${STATE_DIR}/state.json"
VERSION_FILE="${STATE_DIR}/version"
LAST_RUN_FILE="${STATE_DIR}/last-run.json"
LOCK_FILE="/run/lock/proxmox-pve-configuration.lock"
LOG_DIR="/var/log/proxmox-deployer"
CONFIGURATION_LOG_DIR="/var/log/proxmox-bootstrap"
CONFIGURATION_LOG_FILE="${CONFIGURATION_LOG_DIR}/configure-pve.log"
BACKUP_ROOT="/var/backups/proxmox-bootstrap"
SECRETS_BACKUP_ROOT="/var/backups/proxmox-secrets"

KEY_FILE="${SSH_DIR}/github_proxmox_repo_ed25519"
SSH_CONFIG="${SSH_DIR}/config"
KNOWN_HOSTS="${SSH_DIR}/known_hosts"
PVE_GUEST_KEY="${SSH_DIR}/pve_guest_ed25519"
PVE_GUEST_PUB="${PVE_GUEST_KEY}.pub"
LEGACY_GUEST_BOOTSTRAP_KEY="${SSH_DIR}/guest_bootstrap_ed25519"
LEGACY_GUEST_BOOTSTRAP_PUB="${LEGACY_GUEST_BOOTSTRAP_KEY}.pub"

TEMPLATE_VMID=9000
TEMPLATE_NAME="tpl-debian13"
TEMPLATE_VERSION=6
MANAGED_POOL="managed"
BRIDGE="vmbr0"
LXC_TEMPLATE_STORAGE="local"
TEMPLATE_MIN_LOCAL_BYTES=$((2 * 1024 * 1024 * 1024))
TEMPLATE_MIN_DISK_STORAGE_BYTES=$((18 * 1024 * 1024 * 1024))

UPDATE_SYSTEM=0
WARN_COUNT=0
REPO_REVISION=""
CONFIGURATION_RUNNING=0
API_HEADER_FILE=""
CANONICAL_KEY_DIFFERS_FROM_BOOTSTRAP=0
LXC_TEMPLATE_VOLUME=""

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
die() {
    local message=$*
    printf '\n%s%sОШИБКА:%s %s\n' "$C_BOLD" "$C_RED" "$C_RESET" "$message" >&2
    if (( CONFIGURATION_RUNNING )); then
        trap - ERR
        write_state "failed" "$REPO_REVISION" >/dev/null 2>&1 || true
    fi
    exit 1
}

usage() {
    cat <<'USAGE'
Использование:
  configure-pve.sh [--update-system] [--help]

PVE Configuration — повторяемое приведение Proxmox VE к конфигурации проекта.
Обычно запускается автоматически через публичный bootstrap-pve.sh после получения
или обновления приватного репозитория zsergeyru/proxmox.

Параметры:
  --update-system  дополнительно выполнить apt full-upgrade Proxmox/Debian
  -h, --help       показать эту справку
USAGE
}

parse_configuration_args() {
    while (($#)); do
        case "$1" in
            --update-system) UPDATE_SYSTEM=1 ;;
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

on_error() {
    local rc=$?
    trap - ERR
    if (( CONFIGURATION_RUNNING )); then
        write_state "failed" "$REPO_REVISION" >/dev/null 2>&1 || true
    fi
    printf '\n%s%sPVE Configuration аварийно остановлена.%s Код возврата: %s.\n' "$C_BOLD" "$C_RED" "$C_RESET" "$rc" >&2
    exit "$rc"
}

on_signal() {
    local rc=$1 signal_name=$2
    trap - ERR INT TERM
    cleanup_api_header_file
    if (( CONFIGURATION_RUNNING )); then
        write_state "interrupted" "$REPO_REVISION" >/dev/null 2>&1 || true
    fi
    printf '\n%s%sPVE Configuration прервана сигналом %s.%s\n' "$C_BOLD" "$C_RED" "$signal_name" "$C_RESET" >&2
    exit "$rc"
}

install_configuration_traps() {
    trap on_error ERR
    trap cleanup_api_header_file EXIT
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
    exec 9>"$LOCK_FILE"
    flock -n 9 || die "Другой экземпляр configure-pve.sh уже выполняется. Параллельный запуск запрещён."
    ok "Получена эксклюзивная блокировка PVE Configuration"
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

    cat >"$LAST_RUN_FILE" <<EOF_LAST
{
  "timestamp": "${now}",
  "component": "pve-configuration",
  "result": "${status}",
  "repository_revision": "${revision}",
  "warnings": ${WARN_COUNT}
}
EOF_LAST

    printf '%s\n' "$PVE_CONFIGURATION_VERSION" >"$VERSION_FILE"
    chmod 0644 "$STATE_FILE" "$LAST_RUN_FILE" "$VERSION_FILE"
}

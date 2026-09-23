#!/usr/bin/env bash
set -Eeuo pipefail

# Каноническая повторяемая конфигурация Proxmox VE.
# Public Bootstrap только получает/обновляет private repo и передаёт управление сюда.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="${SCRIPT_DIR}/lib"
SOURCE_ROOT="$(cd -- "${SCRIPT_DIR}/../../.." && pwd)"
PRE_SOURCE_LOCK_FILE="/run/lock/proxmox-orchestration.lock"

pre_source_die() {
    printf 'ОШИБКА: %s\n' "$*" >&2
    exit 1
}

acquire_pre_source_lock() {
    command -v flock >/dev/null 2>&1 \
        || pre_source_die "Не найдена команда flock; PVE Configuration не может безопасно продолжить"
    install -d -m 0755 /run/lock

    if [[ "${PVE_ORCHESTRATION_LOCK_HELD:-0}" == "1" ]]; then
        [[ -e /proc/$$/fd/9 ]] \
            || pre_source_die "Public Bootstrap сообщил об унаследованной orchestration lock, но fd 9 отсутствует"
        local target
        target="$(readlink "/proc/$$/fd/9" 2>/dev/null || true)"
        [[ "$target" == "$PRE_SOURCE_LOCK_FILE" ]] \
            || pre_source_die "Унаследованный fd 9 указывает на '${target:-неизвестно}', ожидается ${PRE_SOURCE_LOCK_FILE}"
        flock -n 9 \
            || pre_source_die "Унаследованный fd 9 не удерживает доступную orchestration lock; параллельный run уже активен"
        return
    fi

    exec 9>"$PRE_SOURCE_LOCK_FILE"
    flock -n 9 \
        || pre_source_die "Другой Public Bootstrap или PVE Configuration уже выполняется; параллельный запуск запрещён"
    export PVE_ORCHESTRATION_LOCK_HELD=1
}

verify_source_checkout_before_source() {
    [[ $EUID -eq 0 ]] \
        || pre_source_die "PVE Configuration должна запускаться от root; проверка выполняется до загрузки lib/*.sh"
    command -v git >/dev/null 2>&1 \
        || pre_source_die "Не найдена команда git; невозможно проверить revision исполняемого PVE Configuration"
    command -v find >/dev/null 2>&1 \
        || pre_source_die "Не найдена команда find; невозможно проверить ownership исполняемого checkout"

    git -C "$SOURCE_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
        || pre_source_die "${SOURCE_ROOT} не является Git worktree"

    local actual expected top status trust_violation source_parent parent_owner parent_mode
    top="$(git -C "$SOURCE_ROOT" rev-parse --show-toplevel 2>/dev/null)"
    [[ "$top" == "$SOURCE_ROOT" ]] \
        || pre_source_die "PVE Configuration должна запускаться из корня checkout ${SOURCE_ROOT}, Git сообщает ${top:-неизвестно}"

    source_parent="$(dirname -- "$SOURCE_ROOT")"
    parent_owner="$(stat -c '%U' "$source_parent" 2>/dev/null || true)"
    parent_mode="$(stat -c '%A' "$source_parent" 2>/dev/null || true)"
    [[ "$parent_owner" == "root" ]] \
        || pre_source_die "Родитель исполняемого checkout ${source_parent} должен принадлежать root; обнаружено ${parent_owner:-неизвестно}"
    [[ ${#parent_mode} -ge 9 && "${parent_mode:5:1}" != "w" && "${parent_mode:8:1}" != "w" ]] \
        || pre_source_die "Родитель исполняемого checkout ${source_parent} writable для group/other (${parent_mode:-неизвестно}); весь repo можно подменить целиком"

    trust_violation="$(find "$SOURCE_ROOT" -xdev \( -type f -o -type d \) \( ! -uid 0 -o -perm /022 \) -print -quit 2>/dev/null || true)"
    [[ -z "$trust_violation" ]] \
        || pre_source_die "Исполняемый checkout не является root-trusted: '${trust_violation}' не root-owned или доступен на запись группе/остальным. Запустите Public Bootstrap для безопасной миграции canonical source."

    actual="$(git -C "$SOURCE_ROOT" rev-parse HEAD 2>/dev/null)"
    expected="${PVE_CONFIGURATION_SOURCE_REVISION:-$actual}"
    [[ "$expected" =~ ^[0-9a-f]{40}$ ]] \
        || pre_source_die "Некорректная source revision PVE Configuration: '${expected:-не задана}'"
    [[ "$actual" == "$expected" ]] \
        || pre_source_die "Исполняемый checkout находится на ${actual}, а текущий run зафиксирован на ${expected}"

    status="$(git -C "$SOURCE_ROOT" status --porcelain=v1 --untracked-files=all --ignored)"
    status="$(printf '%s\n' "$status" | grep -Ev '^!! .*(__pycache__/|\.py[co]$)' || true)"
    [[ -z "$status" ]] \
        || pre_source_die "Исполняемый Git checkout содержит tracked/staged/untracked/ignored drift; run остановлен, чтобы SHA соответствовал реально исполняемому коду. Первый элемент: $(head -n1 <<<"$status")"

    export PVE_CONFIGURATION_SOURCE_REVISION="$expected"
}

acquire_pre_source_lock
verify_source_checkout_before_source

for module in \
    00-common.sh \
    10-preflight.sh \
    20-system.sh \
    30-storage.sh \
    40-runtime.sh \
    45-management-keys.sh \
    50-access.sh \
    55-infra-manager.sh \
    60-template-contract.sh \
    61-template-source.sh \
    62-template-build.sh \
    63-template-smoke.sh \
    64-cloud-init-status.sh \
    70-tooling.sh \
    71-sync-management-keys-tooling.sh; do
    [[ -f "${LIB_DIR}/${module}" ]] || {
        printf 'ОШИБКА: не найден модуль PVE Configuration: %s\n' "${LIB_DIR}/${module}" >&2
        exit 1
    }
    # shellcheck source=/dev/null
    source "${LIB_DIR}/${module}"
done

configuration_banner_border() {
    local left=$1 right=$2 rule=''
    printf -v rule '%*s' 68 ''
    rule=${rule// /─}
    printf '%s%s%s\n' "$left" "$rule" "$right"
}

configuration_banner_line() {
    local text=$1 width=66 pad=''
    local LC_ALL=C.UTF-8
    if (( ${#text} > width )); then
        printf '│ %s │\n' "$text"
        return
    fi
    printf -v pad '%*s' "$((width - ${#text}))" ''
    printf '│ %s%s │\n' "$text" "$pad"
}

configuration_mode() {
    printf '%s%s[РЕЖИМ]%s %s\n' "$C_BOLD" "$C_MAGENTA" "$C_RESET" "$*"
}

show_configuration_banner() {
    printf '\n%s%s' "$C_BOLD" "$C_CYAN"
    configuration_banner_border '┌' '┐'
    configuration_banner_line 'Proxmox Project — PVE Configuration'
    configuration_banner_line ''
    configuration_banner_line 'Проверяет и настраивает Proxmox host, доступы, storage,'
    configuration_banner_line 'Debian template 9000 и инфраструктуру deployment.'
    configuration_banner_line ''
    configuration_banner_line "PVE Configuration: v${PVE_CONFIGURATION_VERSION}"
    configuration_banner_border '└' '┘'
    printf '%s' "$C_RESET"

    if (( UPDATE_SYSTEM )); then
        configuration_mode 'Включено полное обновление Proxmox/Debian'
    fi
}

parse_configuration_args "$@"
install_configuration_traps

main() {
    setup_configuration_log
    show_configuration_banner
    acquire_configuration_lock

    CONFIGURATION_RUNNING=1
    write_state "running" "$RUN_SOURCE_REVISION"

    check_root_and_pve
    snapshot_host_config
    configure_apt
    install_packages
    check_time_dns_network

    ensure_storage_layout
    ensure_lxc_template

    ensure_runtime_layout
    ensure_pve_deployer_key
    ensure_management_public_key_registry
    prepare_canonical_github_access
    verify_private_repo_access
    sync_private_repo

    ensure_managed_pool
    ensure_roles
    ensure_pve_identities

    ensure_infra_manager


    install_private_tooling
    install_sync_management_keys_tooling
    report_status

    CONFIGURATION_RUNNING=0
}

main
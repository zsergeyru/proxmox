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
    command -v git >/dev/null 2>&1 \
        || pre_source_die "Не найдена команда git; невозможно проверить revision исполняемого PVE Configuration"

    git -C "$SOURCE_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
        || pre_source_die "${SOURCE_ROOT} не является Git worktree"

    local actual expected top status
    top="$(git -C "$SOURCE_ROOT" rev-parse --show-toplevel 2>/dev/null)"
    [[ "$top" == "$SOURCE_ROOT" ]] \
        || pre_source_die "PVE Configuration должна запускаться из корня checkout ${SOURCE_ROOT}, Git сообщает ${top:-неизвестно}"

    actual="$(git -C "$SOURCE_ROOT" rev-parse HEAD 2>/dev/null)"
    expected="${PVE_CONFIGURATION_SOURCE_REVISION:-$actual}"
    [[ "$expected" =~ ^[0-9a-f]{40}$ ]] \
        || pre_source_die "Некорректная source revision PVE Configuration: '${expected:-не задана}'"
    [[ "$actual" == "$expected" ]] \
        || pre_source_die "Исполняемый checkout находится на ${actual}, а текущий run зафиксирован на ${expected}"

    status="$(git -C "$SOURCE_ROOT" status --porcelain=v1 --untracked-files=all --ignored)"
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
    50-access.sh \
    60-template-contract.sh \
    61-template-source.sh \
    62-template-build.sh \
    70-tooling.sh; do
    [[ -f "${LIB_DIR}/${module}" ]] || {
        printf 'ОШИБКА: не найден модуль PVE Configuration: %s\n' "${LIB_DIR}/${module}" >&2
        exit 1
    }
    # shellcheck source=/dev/null
    source "${LIB_DIR}/${module}"
done

parse_configuration_args "$@"
install_configuration_traps

main() {
    [[ $EUID -eq 0 ]] || die "Запустите configure-pve.sh от root на хосте Proxmox"

    setup_configuration_log
    acquire_configuration_lock

    CONFIGURATION_RUNNING=1
    write_state "running" "$RUN_SOURCE_REVISION"

    check_root_and_pve
    snapshot_host_config
    check_template_state
    ensure_template_protection

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

    if (( TEMPLATE_BUILD_REQUIRED )); then
        prepare_template_source
        create_template_builder
        provision_template_builder
        verify_template_builder
        finalize_template_builder
        seal_template
    fi

    verify_template_contract

    install_private_tooling
    report_status

    CONFIGURATION_RUNNING=0
}

main

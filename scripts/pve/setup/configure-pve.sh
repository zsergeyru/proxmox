#!/usr/bin/env bash
set -Eeuo pipefail

# Каноническая повторяемая конфигурация Proxmox VE.
# Public Bootstrap только получает/обновляет private repo и передаёт управление сюда.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="${SCRIPT_DIR}/lib"

for module in \
    00-common.sh \
    10-preflight.sh \
    20-system.sh \
    30-storage.sh \
    40-runtime.sh \
    50-access.sh \
    70-template-tooling.sh; do
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

    CONFIGURATION_RUNNING=0
}

main

#!/usr/bin/env bash
set -Eeuo pipefail

case "$(basename "$0")" in
    infra-manager-status)
        COMMAND="status"
        ;;
    infra-manager-pve-access-check)
        COMMAND="pve-access-check"
        ;;
    infra-manager-pve-lifecycle-test)
        COMMAND="pve-lifecycle-test"
        ;;
    *)
        printf 'ОШИБКА: неизвестное имя команды infra-manager: %s\n' "$(basename "$0")" >&2
        exit 2
        ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [[ -d "${LOCAL_ROOT}/infra_manager" ]]; then
    PYTHON_ROOT="${LOCAL_ROOT}"
elif [[ -d "/usr/local/lib/infra-manager/infra_manager" ]]; then
    PYTHON_ROOT="/usr/local/lib/infra-manager"
else
    PYTHON_ROOT="/var/lib/infra-manager/bootstrap-repo/scripts/infra-manager"
fi

[[ -d "${PYTHON_ROOT}/infra_manager" ]] || {
    printf 'ОШИБКА: не найден Python package infra_manager: %s\n' "${PYTHON_ROOT}" >&2
    exit 1
}

export PYTHONPATH="${PYTHON_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m infra_manager "${COMMAND}" "$@"

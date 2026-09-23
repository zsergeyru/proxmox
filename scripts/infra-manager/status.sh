#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -d "${SCRIPT_DIR}/infra_manager" ]]; then
    PYTHON_ROOT="${SCRIPT_DIR}"
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
exec python3 -m infra_manager status "$@"

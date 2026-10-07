#!/usr/bin/env bash
set -Eeuo pipefail

PYTHON_ROOT="/usr/local/lib/infra-manager"
[[ -d "${PYTHON_ROOT}/infra_manager" ]] || {
    printf 'ОШИБКА: не найден Python package infra_manager: %s\n' "${PYTHON_ROOT}" >&2
    exit 1
}

export PYTHONPATH="${PYTHON_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m infra_manager.operator "$@"

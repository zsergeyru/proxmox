#!/usr/bin/env bash
set -Eeuo pipefail

# Передать команду оператора в установленный Python-пакет infra-manager.
# Сценарий не изменяет состояние сам и завершается с кодом Python-команды.

PYTHON_ROOT="/usr/local/lib/infra-manager"
[[ -d "${PYTHON_ROOT}/infra_manager" ]] || {
    printf 'ОШИБКА: не найден пакет Python infra_manager (проверьте установку управляющей среды): %s\n' "${PYTHON_ROOT}" >&2
    exit 1
}

export PYTHONPATH="${PYTHON_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m infra_manager.operator "$@"

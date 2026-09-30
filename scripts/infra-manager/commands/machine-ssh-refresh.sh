#!/usr/bin/env bash
set -Eeuo pipefail

PYTHON_ROOT="/usr/local/lib/infra-manager"
REPO_ROOT="${1:-/var/lib/infra-manager/bootstrap-repo}"

[[ -d "${PYTHON_ROOT}/infra_manager" ]] || {
    printf 'ОШИБКА: не найден Python package infra_manager: %s\n' "${PYTHON_ROOT}" >&2
    exit 1
}
[[ -f "${REPO_ROOT}/infrastructure/security/access.yaml" ]] || {
    printf 'ОШИБКА: не найден access.yaml в %s\n' "${REPO_ROOT}" >&2
    exit 1
}

export PYTHONPATH="${PYTHON_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
exec python3 -m infra_manager.machine_ssh --repo-root "${REPO_ROOT}"

#!/usr/bin/env bash
set -Eeuo pipefail

# Минимальный Debian может не содержать Python до первого bootstrap.
# После появления интерпретатора вся дальнейшая настройка выполняется Python.
export LANG=C.UTF-8
export LC_ALL=C.UTF-8

[[ $EUID -eq 0 ]] || { echo "ОШИБКА: setup.sh должен выполняться от root" >&2; exit 1; }

if ! command -v python3 >/dev/null 2>&1; then
    apt-get update
    env DEBIAN_FRONTEND=noninteractive \
        apt-get install -y --no-install-recommends python3
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

exec python3 -m infra_manager setup "$@"

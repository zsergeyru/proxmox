#!/usr/bin/env bash
set -Eeuo pipefail

# Совместимость со старым private path.
# Каноническая команда: scripts/pve/setup/configure-pve.sh

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${SCRIPT_DIR}/../setup/configure-pve.sh"

[[ -f "$TARGET" ]] || {
    printf 'ОШИБКА: не найдена каноническая PVE Configuration: %s\n' "$TARGET" >&2
    exit 1
}

if [[ -t 2 && -z "${NO_COLOR:-}" && "${TERM:-dumb}" != "dumb" ]]; then
    printf '\033[1;33m[ПРЕДУПРЕЖДЕНИЕ]\033[0m scripts/pve/bootstrap/init-pve.sh устарел; используйте scripts/pve/setup/configure-pve.sh\n' >&2
else
    printf '[ПРЕДУПРЕЖДЕНИЕ] scripts/pve/bootstrap/init-pve.sh устарел; используйте scripts/pve/setup/configure-pve.sh\n' >&2
fi

exec bash "$TARGET" "$@"

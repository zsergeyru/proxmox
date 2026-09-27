#!/usr/bin/env bash
set -Eeuo pipefail

[[ $EUID -eq 0 ]] || { echo "ОШИБКА: сценарий должен выполняться от root внутри LXC 990" >&2; exit 1; }

REPO_ROOT="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
RUN_RUNTIME="$REPO_ROOT/scripts/bootstrap-runner/run-runtime.sh"
STATE_FILE="/var/lib/bootstrap-runner/opentofu/state/proxmox.tfstate"
EXPECTED_TARGET='proxmox_virtual_environment_container.guest["910"]'

[[ -x "$RUN_RUNTIME" || -f "$RUN_RUNTIME" ]] || {
    echo "ОШИБКА: не найден run-runtime.sh: $RUN_RUNTIME" >&2
    exit 1
}

bash "$RUN_RUNTIME"     python3 scripts/infra-manager/jobs/deploy-guest.py 910 --bootstrap-scope

[[ -s "$STATE_FILE" ]] || {
    echo "ОШИБКА: после deploy-guest отсутствует состояние 990: $STATE_FILE" >&2
    exit 1
}

mapfile -t resources < <(
    bash "$RUN_RUNTIME"         tofu -chdir=automation/opentofu state list
)

if [[ "${#resources[@]}" -ne 1 || "${resources[0]}" != "$EXPECTED_TARGET" ]]; then
    printf 'ОШИБКА: состояние 990 должно содержать только %s\n' "$EXPECTED_TARGET" >&2
    printf 'Фактические ресурсы:\n' >&2
    printf '  %s\n' "${resources[@]:-(пусто)}" >&2
    exit 1
fi

printf '[ОК] 910 создан через отдельное состояние bootstrap-runner\n'

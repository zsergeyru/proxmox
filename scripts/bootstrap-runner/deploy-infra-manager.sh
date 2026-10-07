#!/usr/bin/env bash
set -Eeuo pipefail

[[ $EUID -eq 0 ]] || { echo "ОШИБКА: сценарий должен выполняться от root внутри LXC 990" >&2; exit 1; }

STEP="${1:-}"
INFRA_VMID="${2:-}"
REPO_ROOT="${3:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
RUN_RUNTIME="$REPO_ROOT/scripts/bootstrap-runner/run-runtime.sh"
STATE_FILE="/var/lib/bootstrap-runner/opentofu/state/proxmox.tfstate"

[[ "$INFRA_VMID" =~ ^[0-9]+$ ]] || {
    echo "ОШИБКА: не задан корректный VMID infra-manager" >&2
    exit 2
}

EXPECTED_TARGET="proxmox_virtual_environment_container.guest[\"${INFRA_VMID}\"]"

[[ -x "$RUN_RUNTIME" || -f "$RUN_RUNTIME" ]] || {
    echo "ОШИБКА: не найден run-runtime.sh: $RUN_RUNTIME" >&2
    exit 1
}

case "$STEP" in
    create)
        bash "$RUN_RUNTIME" python3 scripts/infra-manager/jobs/bootstrap-infra-manager.py create "$INFRA_VMID"
        ;;
    configure-base)
        bash "$RUN_RUNTIME" python3 scripts/infra-manager/jobs/bootstrap-infra-manager.py configure-base "$INFRA_VMID"
        ;;
    configure)
        bash "$RUN_RUNTIME" python3 scripts/infra-manager/jobs/bootstrap-infra-manager.py configure "$INFRA_VMID"
        ;;
    *)
        echo "Использование: deploy-infra-manager.sh create|configure-base|configure VMID [REPO_ROOT]" >&2
        exit 2
        ;;
esac

    [[ -s "$STATE_FILE" ]] || {
        echo "ОШИБКА: отсутствует состояние bootstrap-runner: $STATE_FILE" >&2
        exit 1
    }

    mapfile -t resources < <(
        bash "$RUN_RUNTIME" tofu -chdir=automation/opentofu state list
    )

    if [[ "${#resources[@]}" -ne 1 || "${resources[0]}" != "$EXPECTED_TARGET" ]]; then
        printf 'ОШИБКА: состояние bootstrap-runner должно содержать только %s\n' "$EXPECTED_TARGET" >&2
        printf 'Фактические ресурсы:\n' >&2
        printf '  %s\n' "${resources[@]:-(пусто)}" >&2
        exit 1
    fi


case "$STEP" in
    create)
        printf '[ОК] Основа LXC %s infra-manager готова\n' "$INFRA_VMID"
        ;;
    configure-base)
        printf '[ОК] Базовая настройка %s infra-manager завершена\n' "$INFRA_VMID"
        ;;
    configure)
        printf '[ОК] Полная настройка %s infra-manager завершена\n' "$INFRA_VMID"
        ;;
esac

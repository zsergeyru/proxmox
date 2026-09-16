#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"
# shellcheck source=/dev/null
source "${ROOT}/scripts/pve/setup/lib/62-template-build.sh"

TEMPLATE_VMID=9000
TEMPLATE_WAIT_SECONDS=1200
QM_CASE=success

qm() {
    case "$QM_CASE" in
        success)
            printf '%s\n' '{"pid":321,"exited":1,"exitcode":0,"out-data":"EXEC_OK\n"}'
            ;;
        timeout)
            printf '%s\n' '{"pid":322}'
            ;;
        running)
            printf '%s\n' '{"pid":323,"exited":0}'
            ;;
        failed)
            printf '%s\n' '{"pid":324,"exited":1,"exitcode":7,"err-data":"guest failed"}'
            ;;
        signaled)
            printf '%s\n' '{"pid":325,"exited":1,"signal":15}'
            ;;
        direct)
            printf '%s\n' 'DIRECT_OK'
            ;;
        *) return 2 ;;
    esac
}

out="$(template_guest_exec /bin/true)"
[[ "$out" == "EXEC_OK" ]]

QM_CASE=direct
out="$(template_guest_exec /bin/true)"
[[ "$out" == "DIRECT_OK" ]]

for QM_CASE in timeout running failed signaled; do
    if template_guest_exec /bin/false >/dev/null 2>&1; then
        printf 'Expected template_guest_exec failure for case %s\n' "$QM_CASE" >&2
        exit 1
    fi
done

[[ "$(template_stage_label base-packages)" == 'Базовые пакеты — установка инструментов и служб' ]]
[[ "$(template_stage_label services)" == 'Сервисы — SSH, QGA, консоли и fstrim' ]]
[[ "$(template_stage_label unexpected-stage)" == 'Неизвестный этап (unexpected-stage)' ]]

printf 'Template guest exec tests passed.\n'

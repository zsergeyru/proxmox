#!/usr/bin/env bash
set -Eeuo pipefail

# Каноническая точка входа private Stage 1.
# Реализация bootstrap хранится в init-pve-core.sh; эта тонкая оболочка отвечает
# только за красивый терминальный вывод и не меняет содержимое plain-text логов.

BOOTSTRAP_VERSION=12

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CORE="${SCRIPT_DIR}/init-pve-core.sh"

[[ -f "$CORE" ]] || {
    printf 'ОШИБКА: не найден private Stage 1 core: %s\n' "$CORE" >&2
    exit 1
}

# При редиректе, неизвестном TERM или NO_COLOR=1 оставляем исходный plain text.
if [[ ! -t 1 || -n "${NO_COLOR:-}" || "${TERM:-dumb}" == "dumb" ]]; then
    exec bash "$CORE" "$@"
fi

# Core самостоятельно пишет plain-text лог в /var/log/proxmox-bootstrap/init-pve.log.
# Здесь раскрашивается только поток, который уже выходит в интерактивный терминал.
set +e
bash "$CORE" "$@" 2>&1 | awk '
BEGIN {
    esc = sprintf("%c", 27)
    reset   = esc "[0m"
    bold    = esc "[1m"
    blue    = esc "[34m"
    green   = esc "[32m"
    yellow  = esc "[33m"
    red     = esc "[31m"
    cyan    = esc "[36m"
    magenta = esc "[35m"
}
{
    line = $0

    if (line ~ /^==> /) {
        print bold blue line reset
    } else if (line ~ /^\[ОК\]/) {
        print bold green line reset
    } else if (line ~ /^\[ПРЕДУПРЕЖДЕНИЕ\]/) {
        print bold yellow line reset
    } else if (line ~ /^\[ИНФО\]/) {
        print bold cyan line reset
    } else if (line ~ /^\[ОЖИДАНИЕ\]/) {
        print bold yellow line reset
    } else if (line ~ /^\[НЕТ\]/) {
        print bold red line reset
    } else if (line ~ /^ОШИБКА:/ || line ~ /аварийно остановлена/ || line ~ /прервана сигналом/) {
        print bold red line reset
    } else if (line ~ /^СОСТОЯНИЕ ПРИВАТНОЙ ИНИЦИАЛИЗАЦИИ PVE$/) {
        print bold cyan line reset
    } else if (line ~ /^ГОТОВО$/) {
        print bold green line reset
    } else if (line ~ /^ГОТОВО С ПРЕДУПРЕЖДЕНИЯМИ$/) {
        print bold yellow line reset
    } else if (line ~ /^ЧАСТИЧНО:/) {
        print bold magenta line reset
    } else {
        print line
    }
    fflush()
}'
status=("${PIPESTATUS[@]}")
set -e

# Если colorizer сам аварийно завершился, это важнее результата core.
if (( status[1] != 0 )); then
    exit "${status[1]}"
fi
exit "${status[0]}"

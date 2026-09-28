#!/usr/bin/env bash
set -Eeuo pipefail

COMPOSE_DIR="/opt/infra-manager/compose"
LOG_DIR="/var/log/infra-manager"
LOG_FILE="$LOG_DIR/runtime-activation.log"
BRANCH="${INFRA_PROJECT_BRANCH:-main}"

install -d -m 0755 "$LOG_DIR"
exec >>"$LOG_FILE" 2>&1

printf '\n[%s] Активация infra-runtime, ветка %s\n' "$(date -Is)" "$BRANCH"

docker compose \
    --env-file "$COMPOSE_DIR/.versions.env" \
    -f "$COMPOSE_DIR/docker-compose.yml" \
    up -d --remove-orphans

for _ in $(seq 1 60); do
    if curl -fsS http://127.0.0.1:3000/api/ping >/dev/null 2>&1; then
        INFRA_PROJECT_BRANCH="$BRANCH" \
            /usr/local/sbin/infra-manager-status --full --quiet
        printf '[ОК] infra-runtime активирован и проверен\n'
        exit 0
    fi
    sleep 2
done

printf 'ОШИБКА: Semaphore не запустился после активации infra-runtime\n' >&2
exit 1

#!/usr/bin/env bash
set -Eeuo pipefail

COMPOSE_DIR="/opt/infra-manager/compose"
LOG_DIR="/var/log/infra-manager"
LOG_FILE="$LOG_DIR/runtime-activation.log"
ACTIVATION_MARKER="/mnt/persistent-state/semaphore/.infra-manager-runtime-activation-pending"
BRANCH="${INFRA_PROJECT_BRANCH:-main}"
PVE_NODE="${INFRA_PVE_NODE:-}"

install -d -m 0755 "$LOG_DIR"
exec >>"$LOG_FILE" 2>&1

cleanup_marker() {
    rm -f "$ACTIVATION_MARKER"
}
trap cleanup_marker EXIT

printf '\n[%s] Активация infra-runtime, ветка %s\n' "$(date -Is)" "$BRANCH"

if [[ -f "$ACTIVATION_MARKER" ]]; then
    read -r deploy_pid < "$ACTIVATION_MARKER" || true
    if [[ ! "$deploy_pid" =~ ^[0-9]+$ ]]; then
        printf 'ОШИБКА: Некорректный PID в маркере активации: %s\n' "$ACTIVATION_MARKER" >&2
        exit 1
    fi

    printf '[ИНФО] Ожидание завершения самообновления infra-manager, PID %s\n' "$deploy_pid"
    deploy_finished=0
    for _ in $(seq 1 900); do
        if ! docker exec --user 0 infra-runtime             sh -c "kill -0 $deploy_pid 2>/dev/null"; then
            deploy_finished=1
            break
        fi
        sleep 1
    done

    if [[ "$deploy_finished" != "1" ]]; then
        printf 'ОШИБКА: Самообновление infra-manager не завершилось за 15 минут\n' >&2
        exit 1
    fi
    printf '[ОК] Задание обновления infra-manager завершено; начинаю активацию\n'
fi

if [[ -z "$PVE_NODE" ]]; then
    printf 'ОШИБКА: Не задан INFRA_PVE_NODE для разблокировки OpenBao\n' >&2
    exit 1
fi

docker compose     --env-file "$COMPOSE_DIR/.versions.env"     -f "$COMPOSE_DIR/docker-compose.yml"     up -d --remove-orphans

/usr/local/sbin/infra-manager-openbao-startup-unseal "$PVE_NODE"

for _ in $(seq 1 60); do
    if curl -fsS http://127.0.0.1:3000/api/ping >/dev/null 2>&1; then
        PYTHONPATH=/usr/local/lib/infra-manager         INFRA_PROJECT_BRANCH="$BRANCH"             python3 -m infra_manager semaphore-project

        if [[ -f /opt/infra-portal/docker-compose.yml ]]; then
            PYTHONPATH=/usr/local/lib/infra-manager                 INFRA_PROJECT_BRANCH="$BRANCH"                 python3 -m infra_manager portal-bookmarks                     --output /etc/infra-portal/homepage/bookmarks.yaml
            docker compose                 -f /opt/infra-portal/docker-compose.yml                 restart homepage
        fi

        INFRA_PROJECT_BRANCH="$BRANCH"             /usr/local/sbin/infra-manager-status --full --quiet
        printf '[ОК] infra-runtime активирован, OpenBao разблокирован, Semaphore синхронизирован и проверен\n'
        exit 0
    fi
    sleep 2
done

printf 'ОШИБКА: Semaphore не запустился после активации infra-runtime\n' >&2
exit 1

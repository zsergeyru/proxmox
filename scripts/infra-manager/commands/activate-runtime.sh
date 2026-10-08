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
started=$SECONDS

finish_activation() {
    local result=$?
    local elapsed=$((SECONDS - started))
    local duration
    printf -v duration '%02d:%02d' "$((elapsed / 60))" "$((elapsed % 60))"
    if ((elapsed >= 3600)); then
        printf -v duration '%02d:%02d:%02d' "$((elapsed / 3600))" "$(((elapsed / 60) % 60))" "$((elapsed % 60))"
    fi

    if ((result == 0)); then
        printf '[ВРЕМЯ] Отложенная активация infra-runtime: %s\n' "$duration"
    else
        printf '[ПРЕРВАНО] Отложенная активация infra-runtime: %s (код %s)\n' "$duration" "$result"
    fi
    rm -f "$ACTIVATION_MARKER"
}
trap finish_activation EXIT

printf '\n[%s] Активация infra-runtime, ветка %s\n' "$(date -Is)" "$BRANCH"

if [[ -f "$ACTIVATION_MARKER" ]]; then
    read -r deploy_ref < "$ACTIVATION_MARKER" || true
    deploy_scope="runtime"
    deploy_pid="$deploy_ref"

    if [[ "$deploy_ref" =~ ^(host|runtime):([0-9]+)$ ]]; then
        deploy_scope="${BASH_REMATCH[1]}"
        deploy_pid="${BASH_REMATCH[2]}"
    elif [[ ! "$deploy_ref" =~ ^[0-9]+$ ]]; then
        printf 'ОШИБКА: Некорректный PID в маркере активации: %s\n' "$ACTIVATION_MARKER" >&2
        exit 1
    fi

    printf '[ИНФО] Ожидание завершения самообновления infra-manager, %s PID %s\n' "$deploy_scope" "$deploy_pid"
    deploy_finished=0
    for _ in $(seq 1 900); do
        if [[ "$deploy_scope" == "host" ]]; then
            if ! kill -0 "$deploy_pid" 2>/dev/null; then
                deploy_finished=1
                break
            fi
        elif ! docker exec --user 0 infra-runtime sh -c "kill -0 $deploy_pid 2>/dev/null"; then
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


        INFRA_PROJECT_BRANCH="$BRANCH"             /usr/local/sbin/infra-manager-status --full --quiet
        printf '[ОК] infra-runtime активирован, OpenBao разблокирован, Semaphore синхронизирован и проверен\n'
        exit 0
    fi
    sleep 2
done

printf 'ОШИБКА: Semaphore не запустился после активации infra-runtime\n' >&2
exit 1

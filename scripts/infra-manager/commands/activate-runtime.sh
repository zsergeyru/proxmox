#!/usr/bin/env bash
set -Eeuo pipefail

# Отложенно активировать рабочую среду infra-manager внутри управляющего гостя.
# Перезапускает состав Docker, проверяет OpenBao и runtime без Semaphore. Журнал
# записывается в /var/log/infra-manager/runtime-activation.log.
# При неуспехе часть контейнеров уже может быть перезапущена: автоматического
# отката нет, повторение допустимо после проверки состояния служб.

COMPOSE_DIR="/opt/infra-manager/compose"
LOG_DIR="/var/log/infra-manager"
LOG_FILE="$LOG_DIR/runtime-activation.log"
ACTIVATION_MARKER="/mnt/persistent-state/operations/activation/runtime-activation-pending"
# До первого переключения старый Docker ещё не видит новый mount operations.
# Маркер в существующем state/locks позволяет завершить работу старого runtime
# без Semaphore и без пересоздания его во время выполняющегося задания.
TRANSITION_MARKER="/mnt/persistent-state/locks/runtime-activation-pending"
MARKER_VERIFIED=0
BRANCH="${INFRA_PROJECT_BRANCH:-main}"
PVE_NODE="${INFRA_PVE_NODE:-}"
OPERATION_ID=""
ROLLBACK_IMAGE=""
ROLLED_BACK=0
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"

# Вывод Docker и служб может содержать конфиденциальные детали.
# Старый журнал также закрываем от других пользователей до перенаправления.
umask 077
install -d -m 0700 "$LOG_DIR"
if [[ -L "$LOG_FILE" || ( -e "$LOG_FILE" && ! -f "$LOG_FILE" ) ]]; then
    printf 'ОШИБКА: Небезопасный файл журнала активации: %s\n' "$LOG_FILE" >&2
    exit 1
fi
touch "$LOG_FILE"
chmod 0600 "$LOG_FILE"
exec >>"$LOG_FILE" 2>&1
started=$SECONDS

# Зафиксировать результат активации и снять отметку ожидающего задания.
# Отметка удаляется и после ошибки, поэтому следующий запуск обязан
# заново проверить состояние служб, а не считать активацию успешной.
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
    if [[ "$result" -ne 0 && -n "$ROLLBACK_IMAGE" ]]; then
        printf '[ИНФО] Попытка вернуть совместимый runtime после неуспешной активации\n'
        version="$(sed -n 's/^RUNTIME_VERSION=//p' "$COMPOSE_DIR/.versions.env" | head -1)"
        if [[ "$version" =~ ^[a-zA-Z0-9_.-]+$ ]] \
            && docker image tag "$ROLLBACK_IMAGE" "infra-runtime:$version" \
            && docker compose --env-file "$COMPOSE_DIR/.versions.env" \
                -f "$COMPOSE_FILE" up -d --no-build --force-recreate runtime \
            && [[ "$(docker inspect -f '{{.Image}}' infra-runtime)" == "$ROLLBACK_IMAGE" ]] \
            && docker exec infra-runtime tofu version >/dev/null 2>&1 \
            && docker exec infra-runtime ansible --version >/dev/null 2>&1 \
            && docker exec infra-runtime packer version >/dev/null 2>&1 \
            && /usr/local/sbin/infra-manager-openbao-startup-unseal "$PVE_NODE" \
            && INFRA_PROJECT_BRANCH="$BRANCH" \
                /usr/local/sbin/infra-manager-status --full --quiet; then
            ROLLED_BACK=1
            printf '[ОК] Прежний совместимый runtime и полный контур проверены; обновление не принято\n'
        else
            printf '[ОШИБКА] Автоматический возврат не подтверждён; требуется PVE CLI\n'
        fi
    fi

    if [[ -n "$OPERATION_ID" ]]; then
        # Успешный итог фиксируется вне Docker до снятия маркера.
        action=(finish "$OPERATION_ID" "$result")
        if [[ "$ROLLED_BACK" == "1" ]]; then
            action=(rolled-back "$OPERATION_ID")
        fi
        if ! PYTHONPATH=/usr/local/lib/infra-manager \
            python3 -m infra_manager.operation_activation "${action[@]}"; then
            printf 'ОШИБКА: Итог активации не сохранён; маркер остаётся для восстановления\\n' >&2
            exit 1
        fi
    fi
    if [[ "$MARKER_VERIFIED" == "1" ]]; then
        rm -f "$ACTIVATION_MARKER"
    fi
}
trap finish_activation EXIT

printf '\n[%s] Активация infra-runtime, ветка %s\n' "$(date -Is)" "$BRANCH"

# Выбираем именно маркер реально выполнявшегося задания. Не удаляем его,
# если обнаружены два конкурирующих запроса или небезопасный тип файла.
if [[ -e "$ACTIVATION_MARKER" || -L "$ACTIVATION_MARKER" ]]; then
    if [[ -e "$TRANSITION_MARKER" || -L "$TRANSITION_MARKER" ]]; then
        printf 'ОШИБКА: Найдены два маркера активации 910; требуется проверка операций\n' >&2
        exit 1
    fi
elif [[ -e "$TRANSITION_MARKER" || -L "$TRANSITION_MARKER" ]]; then
    ACTIVATION_MARKER="$TRANSITION_MARKER"
else
    printf 'ОШИБКА: Не найден общий маркер активации; переключение runtime отменено\n' >&2
    exit 1
fi
if [[ -L "$ACTIVATION_MARKER" || ! -f "$ACTIVATION_MARKER" ]]; then
    printf 'ОШИБКА: Маркер активации не является обычным файлом\n' >&2
    exit 1
fi
if [[ -f "$ACTIVATION_MARKER" ]]; then
    read -r deploy_ref < "$ACTIVATION_MARKER" || true
    deploy_scope="runtime"
    deploy_pid="$deploy_ref"

    if [[ "$deploy_ref" =~ ^(host|runtime):([0-9]+)(:([0-9a-f]{32}))?$ ]]; then
        deploy_scope="${BASH_REMATCH[1]}"
        deploy_pid="${BASH_REMATCH[2]}"
        OPERATION_ID="${BASH_REMATCH[4]:-}"
    elif [[ ! "$deploy_ref" =~ ^[0-9]+$ ]]; then
        printf 'ОШИБКА: Некорректный PID в маркере активации: %s\n' "$ACTIVATION_MARKER" >&2
        exit 1
    fi

    # Только проверенный формат допускает автоматическую очистку маркера.
    MARKER_VERIFIED=1
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
        # Старое задание может продолжаться: не открываем окно повторного deploy.
        MARKER_VERIFIED=0
        printf 'ОШИБКА: Самообновление infra-manager не завершилось за 15 минут\n' >&2
        exit 1
    fi
    printf '[ОК] Задание обновления infra-manager завершено; начинаю активацию\n'
fi

# Маркер позволяет показывать через закрытый посредник журнал только своей
# активации, не смешивая результаты соседних deploy 910.
if [[ -n "$OPERATION_ID" ]]; then
    printf '[ИНФО] Операция активации: %s\n' "$OPERATION_ID"
fi

# Сохранить идентификатор предыдущего поколения до замены образа.
# Первое переключение с Semaphore заведомо несовместимо: автоматического
# возврата к нему не предпринимаем, доступна прямая команда PVE.
if [[ -n "$OPERATION_ID" ]] && docker inspect infra-runtime >/dev/null 2>&1; then
    previous_id="$(docker inspect -f '{{.Image}}' infra-runtime)"
    previous_generation="$(docker image inspect \
        -f '{{index .Config.Labels "org.proxmox.runtime.generation"}}' \
        "$previous_id" 2>/dev/null || true)"
    if [[ "$previous_generation" == "standalone-v1" ]]; then
        ROLLBACK_IMAGE="$previous_id"
    fi
fi

if [[ -z "$PVE_NODE" ]]; then
    printf 'ОШИБКА: Не задан INFRA_PVE_NODE для разблокировки OpenBao\n' >&2
    exit 1
fi

COMPOSE_FILE="$COMPOSE_DIR/docker-compose.yml"
if ! grep -qx 'RUNTIME_MODE=standalone' "$COMPOSE_DIR/.versions.env"; then
    printf 'ОШИБКА: Старый режим запуска больше не поддерживается\\n' >&2
    exit 1
fi
COMPOSE_FILE="$COMPOSE_DIR/docker-compose-standalone.yml"
docker compose \
    --env-file "$COMPOSE_DIR/.versions.env" \
    -f "$COMPOSE_FILE" up -d --no-build --remove-orphans

# Compose пересоздаёт web лишь при изменении образа либо его окружения.
# Второй force-recreate был лишним и сбрасывал активные веб-сессии.

/usr/local/sbin/infra-manager-openbao-startup-unseal "$PVE_NODE"

if grep -qx 'RUNTIME_MODE=standalone' "$COMPOSE_DIR/.versions.env"; then
    for _ in $(seq 1 60); do
        if docker exec infra-runtime tofu version >/dev/null 2>&1 \
            && docker exec infra-runtime ansible --version >/dev/null 2>&1 \
            && docker exec infra-runtime packer version >/dev/null 2>&1; then
            if [[ -n "$OPERATION_ID" ]]; then
                PYTHONPATH=/usr/local/lib/infra-manager \
                    python3 -m infra_manager.operation_activation verifying "$OPERATION_ID"
            fi
            INFRA_PROJECT_BRANCH="$BRANCH" \
                /usr/local/sbin/infra-manager-status --full --quiet
            printf '[ОК] Независимый infra-runtime и OpenBao проверены без Semaphore\\n'
            exit 0
        fi
        sleep 2
    done
    printf 'ОШИБКА: Независимый infra-runtime не прошёл проверку готовности\\n' >&2
    exit 1
fi

printf 'ОШИБКА: Постоянная среда обязана использовать RUNTIME_MODE=standalone\\n' >&2
exit 1

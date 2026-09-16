#!/usr/bin/env bash

wait_msg() {
    printf '%s%s[ОЖИДАНИЕ]%s %s\n' "$C_BOLD" "$C_YELLOW" "$C_RESET" "$*"
}

template_vm_stage() {
    printf '%s%s[ЭТАП VM]%s %s\n' "$C_BOLD" "$C_MAGENTA" "$C_RESET" "$*"
}

template_guest_exec() {
    local raw exitcode errdata outdata exited pid signal
    if ! raw="$(qm guest exec "$TEMPLATE_VMID" --timeout "$TEMPLATE_WAIT_SECONDS" -- "$@" 2>&1)"; then
        [[ -n "$raw" ]] && printf '%s\n' "$raw" >&2
        return 1
    fi

    # Structured PVE responses can be either a completed exec-status object or,
    # when the synchronous wait expires, only a PID / exited=0 state. Never
    # treat the latter as guest stdout or as a successful command.
    if jq -e 'type == "object" and (has("out-data") or has("err-data") or has("exitcode") or has("exited") or has("pid") or has("signal"))' \
        >/dev/null 2>&1 <<<"$raw"; then
        exited="$(jq -r '.exited // empty' <<<"$raw")"
        pid="$(jq -r '.pid // empty' <<<"$raw")"
        signal="$(jq -r '.signal // empty' <<<"$raw")"
        exitcode="$(jq -r '.exitcode // empty' <<<"$raw")"
        errdata="$(jq -r '."err-data" // empty' <<<"$raw")"
        outdata="$(jq -r '."out-data" // empty' <<<"$raw")"

        if [[ "$exited" == "0" || "$exited" == "false" || ( -z "$exited" && -n "$pid" && -z "$exitcode" ) ]]; then
            printf 'QEMU Guest Agent command не завершилась за timeout=%ss%s\n' \
                "$TEMPLATE_WAIT_SECONDS" "${pid:+ (pid=${pid})}" >&2
            [[ -n "$errdata" ]] && printf '%s\n' "$errdata" >&2
            return 124
        fi

        if [[ -n "$signal" && "$signal" != "0" ]]; then
            printf 'QEMU Guest Agent command завершилась сигналом %s%s\n' \
                "$signal" "${pid:+ (pid=${pid})}" >&2
            [[ -n "$errdata" ]] && printf '%s\n' "$errdata" >&2
            return 1
        fi

        if [[ -n "$exitcode" ]]; then
            [[ "$exitcode" =~ ^[0-9]+$ ]] || {
                printf 'QEMU Guest Agent вернул некорректный exitcode: %s\n' "$exitcode" >&2
                return 1
            }
            if (( exitcode != 0 )); then
                [[ -n "$errdata" ]] && printf '%s\n' "$errdata" >&2
                return 1
            fi
        elif [[ "$exited" == "1" || "$exited" == "true" ]]; then
            printf 'QEMU Guest Agent сообщил exited=%s без exitcode%s\n' \
                "$exited" "${pid:+ (pid=${pid})}" >&2
            [[ -n "$errdata" ]] && printf '%s\n' "$errdata" >&2
            return 1
        fi

        printf '%s\n' "$outdata"
        return
    fi

    # Compatibility with CLI variants that print guest stdout directly.
    printf '%s\n' "$raw"
}

template_wait_progress() {
    local phase=$1 started=$2 limit=$3 detail=${4:-}
    local elapsed vm_state
    elapsed=$((SECONDS - started))
    vm_state="$(qm status "$TEMPLATE_VMID" 2>/dev/null | awk '{print $2}' || true)"
    [[ -n "$vm_state" ]] || vm_state="unknown"
    wait_msg "${phase}: ${elapsed}s/${limit}s, VM=${vm_state}${detail}"
}

template_wait_for_agent() {
    local phase=${1:-QEMU Guest Agent}
    local started=$SECONDS deadline=$((SECONDS + TEMPLATE_WAIT_SECONDS)) next_report=$SECONDS
    while (( SECONDS < deadline )); do
        if qm agent "$TEMPLATE_VMID" ping >/dev/null 2>&1; then
            ok "${phase} доступен через $((SECONDS - started)) с"
            return 0
        fi
        if (( SECONDS >= next_report )); then
            template_wait_progress "$phase" "$started" "$TEMPLATE_WAIT_SECONDS" ', agent=нет ответа'
            next_report=$((SECONDS + TEMPLATE_WAIT_PROGRESS_SECONDS))
        fi
        sleep 5
    done
    return 1
}

template_read_bootstrap_status() {
    template_guest_exec /bin/cat /var/lib/template-build/bootstrap-status 2>/dev/null \
        | head -n1 || true
}

template_wait_for_bootstrap() {
    local started=$SECONDS deadline=$((SECONDS + TEMPLATE_WAIT_SECONDS)) next_report=$SECONDS
    local out status last_status=''
    while (( SECONDS < deadline )); do
        out="$(template_guest_exec /bin/cat /var/lib/template-build/bootstrap-complete 2>/dev/null || true)"
        if grep -q 'BOOTSTRAP_OK' <<<"$out"; then
            ok "Начальная настройка гостя завершена через $((SECONDS - started)) с"
            return 0
        fi

        status="$(template_read_bootstrap_status || true)"
        if [[ -n "$status" && "$status" != "$last_status" ]]; then
            template_vm_stage "$status"
            last_status="$status"
        fi

        if (( SECONDS >= next_report )); then
            if [[ -n "$status" ]]; then
                template_wait_progress "Начальная настройка гостя" "$started" "$TEMPLATE_WAIT_SECONDS" ", QGA=ok; этап=${status}"
            else
                template_wait_progress "Начальная настройка гостя" "$started" "$TEMPLATE_WAIT_SECONDS" ', QGA=ok; ожидается status от template-bootstrap'
            fi
            next_report=$((SECONDS + TEMPLATE_WAIT_PROGRESS_SECONDS))
        fi
        sleep 5
    done
    return 1
}

template_wait_for_cloud_init() {
    local started=$SECONDS deadline=$((SECONDS + TEMPLATE_WAIT_SECONDS)) next_report=$SECONDS
    local out
    while (( SECONDS < deadline )); do
        out="$(template_guest_exec /bin/bash -lc 'cloud-init status' 2>/dev/null || true)"
        if grep -q 'status: done' <<<"$out"; then
            ok "Cloud-Init завершён через $((SECONDS - started)) с"
            return 0
        fi
        if grep -q 'status: error' <<<"$out"; then
            printf '%s\n' "$out" >&2
            return 1
        fi
        if (( SECONDS >= next_report )); then
            template_wait_progress "Cloud-Init" "$started" "$TEMPLATE_WAIT_SECONDS" ', QGA=ok; status ещё не done'
            next_report=$((SECONDS + TEMPLATE_WAIT_PROGRESS_SECONDS))
        fi
        sleep 5
    done
    return 1
}

template_wait_for_new_boot_id() {
    local previous=$1
    local started=$SECONDS deadline=$((SECONDS + TEMPLATE_WAIT_SECONDS)) next_report=$SECONDS current
    while (( SECONDS < deadline )); do
        current="$(template_guest_exec /bin/cat /proc/sys/kernel/random/boot_id 2>/dev/null || true)"
        if [[ -n "$current" && "$current" != "$previous" ]]; then
            ok "Проверочная перезагрузка завершена через $((SECONDS - started)) с"
            return 0
        fi
        if (( SECONDS >= next_report )); then
            template_wait_progress "Проверочная перезагрузка" "$started" "$TEMPLATE_WAIT_SECONDS" ', ожидается новый boot_id/QGA'
            next_report=$((SECONDS + TEMPLATE_WAIT_PROGRESS_SECONDS))
        fi
        sleep 5
    done
    return 1
}

template_wait_for_stopped() {
    local limit=300 started=$SECONDS deadline=$((SECONDS + 300)) next_report=$SECONDS
    while (( SECONDS < deadline )); do
        if [[ "$(qm status "$TEMPLATE_VMID" | awk '{print $2}')" == "stopped" ]]; then
            ok "VM-сборщик выключена через $((SECONDS - started)) с"
            return 0
        fi
        if (( SECONDS >= next_report )); then
            template_wait_progress "Выключение VM-сборщика" "$started" "$limit"
            next_report=$((SECONDS + TEMPLATE_WAIT_PROGRESS_SECONDS))
        fi
        sleep 3
    done
    return 1
}

create_template_builder() {
    (( TEMPLATE_BUILD_REQUIRED == 1 )) || return 0

    log "Создание временной VM-сборщика ${TEMPLATE_VMID}"
    qm create "$TEMPLATE_VMID" \
        --name "$TEMPLATE_BUILDER_NAME" \
        --description "PVE Configuration Debian 13 template builder; template-version=${TEMPLATE_VERSION}; template-builder-state=building" \
        --ostype l26 \
        --cpu host \
        --sockets 1 \
        --cores "$TEMPLATE_CORES" \
        --memory "$TEMPLATE_MEMORY_MB" \
        --scsihw virtio-scsi-single \
        --net0 "virtio,bridge=${BRIDGE}" \
        --serial0 socket \
        --vga std \
        --agent 1 \
        --onboot 0
    TEMPLATE_BUILD_ACTIVE=1

    log "Импорт системного диска template builder"
    qm importdisk "$TEMPLATE_VMID" "$TEMPLATE_IMAGE_PATH" "$TEMPLATE_DISK_STORAGE"

    local imported_disk
    imported_disk="$(qm config "$TEMPLATE_VMID" | awk -F': ' '/^unused[0-9]+:/ {print $2; exit}')"
    [[ -n "$imported_disk" ]] || die "Импортированный диск не найден в конфигурации VM ${TEMPLATE_VMID}"

    qm set "$TEMPLATE_VMID" --scsi0 "${imported_disk},discard=on,iothread=1,ssd=1"
    qm resize "$TEMPLATE_VMID" scsi0 "$TEMPLATE_DISK_SIZE"
    qm set "$TEMPLATE_VMID" --ide2 "${TEMPLATE_DISK_STORAGE}:cloudinit"
    qm set "$TEMPLATE_VMID" --boot "order=scsi0"
    qm set "$TEMPLATE_VMID" --ipconfig0 ip=dhcp
    qm set "$TEMPLATE_VMID" --cicustom "user=${TEMPLATE_SNIPPET_VOL}"

    ok "VM-сборщик ${TEMPLATE_VMID} создан и подготовлен к первому запуску"
}

provision_template_builder() {
    (( TEMPLATE_BUILD_REQUIRED == 1 )) || return 0

    log "Запуск и provisioning VM-сборщика ${TEMPLATE_VMID}"
    qm start "$TEMPLATE_VMID"

    info "Cloud-Init сначала устанавливает qemu-guest-agent, после чего PVE Configuration начинает видеть внутренние этапы сборки."
    template_wait_for_agent "QEMU Guest Agent" \
        || die "QEMU Guest Agent не стал доступен за ${TEMPLATE_WAIT_SECONDS} секунд"
    template_wait_for_bootstrap \
        || die "Начальная настройка гостя не завершилась за ${TEMPLATE_WAIT_SECONDS} секунд. Проверьте консоль VM ${TEMPLATE_VMID} и журналы cloud-init."
    template_wait_for_cloud_init \
        || die "Cloud-Init не завершился корректно со статусом done"
}

verify_template_builder() {
    (( TEMPLATE_BUILD_REQUIRED == 1 )) || return 0

    log "Проверочная перезагрузка VM-сборщика на обычное ядро Debian"
    local boot_id_before verify_output kernel_token framebuffer_token
    boot_id_before="$(template_guest_exec /bin/cat /proc/sys/kernel/random/boot_id)"
    [[ -n "$boot_id_before" ]] || die "Не удалось получить boot_id VM-сборщика перед перезагрузкой"
    qm reboot "$TEMPLATE_VMID"
    template_wait_for_new_boot_id "$boot_id_before" \
        || die "VM-сборщик не завершила проверочную перезагрузку за ${TEMPLATE_WAIT_SECONDS} секунд"
    template_wait_for_agent "QEMU Guest Agent после перезагрузки" \
        || die "QEMU Guest Agent не подключился повторно после проверочной перезагрузки"

    log "Проверка ядра, framebuffer и консольных служб"
    verify_output="$(template_guest_exec /bin/bash -lc '
set -Eeuo pipefail
kernel="$(uname -r)"
[[ "$kernel" == *-amd64 ]]
[[ "$kernel" != *cloud* ]]
[[ -r /sys/class/graphics/fb0/virtual_size ]]
framebuffer="$(cat /sys/class/graphics/fb0/virtual_size)"
[[ -n "$framebuffer" ]]
[[ "$framebuffer" =~ ^[0-9]+,[0-9]+$ ]]
systemctl is-active --quiet qemu-guest-agent.service
systemctl is-active --quiet getty@tty1.service
systemctl is-active --quiet serial-getty@ttyS0.service
grep -q "^FONTSIZE=\"8x16\"$" /etc/default/console-setup
grep -q -- "--autologin root" /etc/systemd/system/getty@tty1.service.d/autologin.conf
grep -q -- "--autologin root" /etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf
printf "VERIFY_KERNEL=%s VERIFY_FRAMEBUFFER=%s CONSOLES_OK\n" "$kernel" "$framebuffer"
')" || die "Guest verification command завершилась ошибкой"
    grep -q 'CONSOLES_OK' <<<"$verify_output" \
        || die "Проверка консолей после перезагрузки не сообщила об успешном завершении"

    kernel_token="$(grep -oE 'VERIFY_KERNEL=[^[:space:]\"]+' <<<"$verify_output" | head -1)"
    framebuffer_token="$(grep -oE 'VERIFY_FRAMEBUFFER=[0-9]+,[0-9]+' <<<"$verify_output" | head -1)"
    TEMPLATE_KERNEL_VERSION="${kernel_token#*=}"
    TEMPLATE_FRAMEBUFFER_SIZE="${framebuffer_token#*=}"
    [[ -n "$TEMPLATE_KERNEL_VERSION" ]] || die "Проверенная версия ядра не была получена"
    [[ -n "$TEMPLATE_FRAMEBUFFER_SIZE" ]] || die "Проверенный размер framebuffer не был получен"

    ok "Проверки builder пройдены: kernel=${TEMPLATE_KERNEL_VERSION}, framebuffer=${TEMPLATE_FRAMEBUFFER_SIZE}"
}

finalize_template_builder() {
    (( TEMPLATE_BUILD_REQUIRED == 1 )) || return 0

    log "Финальная очистка гостевой системы"
    local finalize_output cloudinit_user_data
    finalize_output="$(template_guest_exec /usr/local/sbin/template-finalize)" \
        || die "Финализация гостя завершилась ошибкой"
    grep -q 'FINALIZE_OK' <<<"$finalize_output" \
        || die "Финализация гостя не сообщила об успешном завершении"

    # Проверяем фактический результат cleanup до превращения VM в template.
    template_guest_exec /bin/bash -lc '
set -Eeuo pipefail
! id debian >/dev/null 2>&1
[[ ! -s /etc/machine-id ]]
[[ ! -e /var/lib/dbus/machine-id ]]
! compgen -G "/etc/ssh/ssh_host_*" >/dev/null
[[ ! -e /root/.ssh ]]
[[ ! -e /var/lib/template-build ]]
[[ ! -e /usr/local/sbin/template-bootstrap ]]
[[ ! -e /usr/local/sbin/template-finalize ]]
printf "CLEANUP_OK\n"
' | grep -q 'CLEANUP_OK' \
        || die "Guest cleanup assertions не пройдены; VM-сборщик оставлена для диагностики"
    ok "Guest cleanup assertions подтверждены"

    log "Выключение VM-сборщика"
    qm shutdown "$TEMPLATE_VMID" --timeout 180 || true
    template_wait_for_stopped || die "VM-сборщик не выключилась корректно"

    log "Переход от builder Cloud-Init к стандартному Cloud-Init шаблона"
    qm set "$TEMPLATE_VMID" --delete cicustom
    qm set "$TEMPLATE_VMID" --ciuser root
    qm set "$TEMPLATE_VMID" --ciupgrade 0
    qm set "$TEMPLATE_VMID" --ipconfig0 ip=dhcp
    qm set "$TEMPLATE_VMID" --name "$TEMPLATE_NAME"
    qm set "$TEMPLATE_VMID" --description "Базовый шаблон Debian 13 (Trixie); template-version=${TEMPLATE_VERSION}; root SSH key-only; VGA/noVNC tty1 с автовходом; резервный serial0; SSH-ключи передаются каждому клону отдельно"

    qm cloudinit update "$TEMPLATE_VMID"
    cloudinit_user_data="$(qm cloudinit dump "$TEMPLATE_VMID" user)"
    if grep -qE 'template-bootstrap|builder-debian13|/usr/local/sbin/template-finalize' <<<"$cloudinit_user_data"; then
        die "После пересоздания Cloud-Init всё ещё содержит временные данные builder-а"
    fi

    rm -f -- "$TEMPLATE_SNIPPET_PATH"
    ok "Builder Cloud-Init удалён, стандартный Cloud-Init шаблона восстановлен"
}

seal_template() {
    (( TEMPLATE_BUILD_REQUIRED == 1 )) || return 0

    log "Преобразование VM ${TEMPLATE_VMID} в защищённый template"
    qm template "$TEMPLATE_VMID"
    TEMPLATE_BUILD_ACTIVE=0
    qm set "$TEMPLATE_VMID" --protection 1

    local config
    config="$(qm config "$TEMPLATE_VMID")"
    validate_template_contract "$config" 1 \
        || die "Созданный template ${TEMPLATE_VMID} не прошёл полный contract check"

    TEMPLATE_BUILD_REQUIRED=0
    ok "Создан ${TEMPLATE_VMID} ${TEMPLATE_NAME} Template-Version ${TEMPLATE_VERSION}"
    [[ -n "$TEMPLATE_KERNEL_VERSION" ]] && info "Проверенное ядро: ${TEMPLATE_KERNEL_VERSION}"
    [[ -n "$TEMPLATE_FRAMEBUFFER_SIZE" ]] && info "Проверенный framebuffer: ${TEMPLATE_FRAMEBUFFER_SIZE}"
}

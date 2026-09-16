#!/usr/bin/env bash

template_csv_has() {
    local value=$1 token=$2
    [[ ",${value}," == *",${token},"* ]]
}

template_size_to_kib() {
    local value=$1 number unit
    [[ "$value" =~ ^([0-9]+)([KMGT])$ ]] || return 1
    number=${BASH_REMATCH[1]}
    unit=${BASH_REMATCH[2]}
    case "$unit" in
        K) printf '%s\n' "$number" ;;
        M) printf '%s\n' "$((number * 1024))" ;;
        G) printf '%s\n' "$((number * 1024 * 1024))" ;;
        T) printf '%s\n' "$((number * 1024 * 1024 * 1024))" ;;
        *) return 1 ;;
    esac
}

validate_template_contract() {
    local config=$1
    local require_protection=${2:-1}
    local errors=""
    local scsi0 ide2 net0 boot onboot disk_size disk_kib required_kib

    template_contract_expect() {
        local pattern=$1 message=$2
        if ! grep -qE "$pattern" <<<"$config"; then
            errors+="${message}"$'\n'
        fi
    }

    template_contract_expect "^name: ${TEMPLATE_NAME}$" "name должен быть ${TEMPLATE_NAME}"
    template_contract_expect '^template: 1$' "объект должен быть template=1"
    template_contract_expect '^ostype: l26$' "ostype должен быть l26"
    template_contract_expect '^cpu: host$' "cpu должен быть host"
    template_contract_expect '^sockets: 1$' "sockets должен быть 1"
    template_contract_expect "^cores: ${TEMPLATE_CORES}$" "cores должен быть ${TEMPLATE_CORES}"
    template_contract_expect "^memory: ${TEMPLATE_MEMORY_MB}$" "memory должен быть ${TEMPLATE_MEMORY_MB} MiB"
    template_contract_expect '^scsihw: virtio-scsi-single$' "scsihw должен быть virtio-scsi-single"
    template_contract_expect '^ciuser: root$' "ciuser должен быть root"
    template_contract_expect '^ciupgrade: 0$' "ciupgrade должен быть 0"
    template_contract_expect '^agent: 1$' "QEMU Guest Agent должен быть включён (agent=1)"
    template_contract_expect '^vga: std$' "vga должен быть std"
    template_contract_expect '^serial0: socket$' "serial0 должен быть socket"
    template_contract_expect '^ipconfig0: ip=dhcp$' "ipconfig0 должен быть ip=dhcp"
    template_contract_expect "template-version=${TEMPLATE_VERSION}" "description должен содержать template-version=${TEMPLATE_VERSION}"

    scsi0="$(sed -n 's/^scsi0: //p' <<<"$config" | head -n1)"
    if [[ -z "$scsi0" ]]; then
        errors+="scsi0 системный диск отсутствует"$'\n'
    else
        [[ "$scsi0" == "${TEMPLATE_DISK_STORAGE}:"* ]] \
            || errors+="scsi0 должен находиться на storage ${TEMPLATE_DISK_STORAGE}"$'\n'
        template_csv_has "$scsi0" 'discard=on' \
            || errors+="scsi0 должен иметь discard=on"$'\n'
        template_csv_has "$scsi0" 'iothread=1' \
            || errors+="scsi0 должен иметь iothread=1"$'\n'
        template_csv_has "$scsi0" 'ssd=1' \
            || errors+="scsi0 должен иметь ssd=1"$'\n'

        disk_size="$(tr ',' '\n' <<<"$scsi0" | sed -n 's/^size=//p' | head -n1)"
        disk_kib="$(template_size_to_kib "$disk_size" 2>/dev/null || true)"
        required_kib="$(template_size_to_kib "$TEMPLATE_DISK_SIZE" 2>/dev/null || true)"
        if [[ -z "$disk_kib" || -z "$required_kib" ]]; then
            errors+="не удалось проверить размер scsi0 (обнаружено '${disk_size:-не задано}', минимум ${TEMPLATE_DISK_SIZE})"$'\n'
        elif (( disk_kib < required_kib )); then
            errors+="scsi0 должен быть не меньше ${TEMPLATE_DISK_SIZE}, обнаружено ${disk_size}"$'\n'
        fi
    fi

    ide2="$(sed -n 's/^ide2: //p' <<<"$config" | head -n1)"
    if [[ -z "$ide2" ]]; then
        errors+="ide2 Cloud-Init drive отсутствует"$'\n'
    else
        [[ "$ide2" == "${TEMPLATE_DISK_STORAGE}:"* ]] \
            || errors+="ide2 Cloud-Init drive должен находиться на storage ${TEMPLATE_DISK_STORAGE}"$'\n'
        template_csv_has "$ide2" 'media=cdrom' \
            || errors+="ide2 должен быть Cloud-Init CD-ROM (media=cdrom)"$'\n'
    fi

    net0="$(sed -n 's/^net0: //p' <<<"$config" | head -n1)"
    if [[ -z "$net0" ]]; then
        errors+="net0 отсутствует"$'\n'
    else
        [[ "$net0" == virtio=* ]] \
            || errors+="net0 должен использовать virtio"$'\n'
        template_csv_has "$net0" "bridge=${BRIDGE}" \
            || errors+="net0 должен использовать bridge=${BRIDGE}"$'\n'
    fi

    boot="$(sed -n 's/^boot: //p' <<<"$config" | head -n1)"
    [[ "$boot" == "order=scsi0" || "$boot" == order=scsi0\;* ]] \
        || errors+="boot order должен начинаться с scsi0"$'\n'

    onboot="$(sed -n 's/^onboot: //p' <<<"$config" | head -n1)"
    [[ -z "$onboot" || "$onboot" == "0" ]] \
        || errors+="onboot должен быть 0 (или отсутствовать как default=0)"$'\n'

    if grep -q '^cicustom:' <<<"$config"; then
        errors+="cicustom builder-а не должен оставаться в template"$'\n'
    fi
    if (( require_protection )) && ! grep -q '^protection: 1$' <<<"$config"; then
        errors+="protection должен быть 1"$'\n'
    fi

    unset -f template_contract_expect

    if [[ -n "$errors" ]]; then
        printf 'Template %s не соответствует contract Template-Version %s:\n' "$TEMPLATE_VMID" "$TEMPLATE_VERSION" >&2
        while IFS= read -r item; do
            [[ -n "$item" ]] && printf '  - %s\n' "$item" >&2
        done <<<"$errors"
        return 1
    fi
}

check_template_state() {
    log "Проверка состояния Debian template ${TEMPLATE_VMID}"

    TEMPLATE_BUILD_REQUIRED=0
    TEMPLATE_NEEDS_PROTECTION=0

    if pct config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        die "VMID ${TEMPLATE_VMID} занят LXC-контейнером; создание шаблона невозможно"
    fi

    if ! qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        TEMPLATE_BUILD_REQUIRED=1
        ok "VMID ${TEMPLATE_VMID} свободен; будет создан новый ${TEMPLATE_NAME}"
        return
    fi

    local config current_name
    config="$(qm config "$TEMPLATE_VMID")"
    current_name="$(awk -F': ' '$1=="name" {print $2; exit}' <<<"$config")"

    if grep -q '^template: 1$' <<<"$config"; then
        validate_template_contract "$config" 0 \
            || die "Существующий VMID ${TEMPLATE_VMID} является template, но не соответствует contract ${TEMPLATE_NAME} Template-Version ${TEMPLATE_VERSION}. Автоматическая миграция запрещена."

        if grep -q '^protection: 1$' <<<"$config"; then
            validate_template_contract "$config" 1 \
                || die "Защищённый template ${TEMPLATE_VMID} не прошёл полный contract check"
            ok "Шаблон ${TEMPLATE_VMID} ${TEMPLATE_NAME} Template-Version ${TEMPLATE_VERSION} уже готов"
        else
            TEMPLATE_NEEDS_PROTECTION=1
            info "Шаблон ${TEMPLATE_VMID} соответствует contract, но protection=1 отсутствует; защита будет восстановлена"
        fi
        return
    fi

    if [[ "$current_name" == "$TEMPLATE_BUILDER_NAME" ]] \
        || grep -q 'template-builder-state=building' <<<"$config" \
        || { [[ "$current_name" == "$TEMPLATE_NAME" ]] \
            && grep -q "template-version=${TEMPLATE_VERSION}" <<<"$config" \
            && grep -q '^ciuser: root$' <<<"$config"; }; then
        die "VMID ${TEMPLATE_VMID} содержит незавершённую VM-сборщик '${current_name:-без имени}' от предыдущей попытки. Она намеренно сохранена для диагностики. Проверьте VM, Cloud-Init/QGA и временный snippet ${TEMPLATE_SNIPPET_VOL}; после осознанной очистки VMID ${TEMPLATE_VMID} повторите PVE Configuration."
    fi

    die "VMID ${TEMPLATE_VMID} уже занят обычной VM '${current_name:-без имени}'. PVE Configuration не будет изменять или удалять её автоматически."
}

ensure_template_protection() {
    (( TEMPLATE_BUILD_REQUIRED == 0 )) || return 0
    (( TEMPLATE_NEEDS_PROTECTION == 1 )) || return 0

    log "Восстановление protection для template ${TEMPLATE_VMID}"
    qm set "$TEMPLATE_VMID" --protection 1
    TEMPLATE_NEEDS_PROTECTION=0

    local config
    config="$(qm config "$TEMPLATE_VMID")"
    validate_template_contract "$config" 1 \
        || die "После включения protection шаблон ${TEMPLATE_VMID} не прошёл полный contract check"
    ok "Для шаблона ${TEMPLATE_VMID} включён protection=1"
}

verify_template_contract() {
    local config
    qm config "$TEMPLATE_VMID" >/dev/null 2>&1 \
        || die "VMID ${TEMPLATE_VMID} отсутствует после template pipeline"

    config="$(qm config "$TEMPLATE_VMID")"
    validate_template_contract "$config" 1 \
        || die "VMID ${TEMPLATE_VMID} не прошёл итоговый contract check"

    ok "Итоговый contract шаблона ${TEMPLATE_VMID} ${TEMPLATE_NAME} Template-Version ${TEMPLATE_VERSION} подтверждён"
}

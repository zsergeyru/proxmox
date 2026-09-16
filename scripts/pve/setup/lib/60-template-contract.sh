#!/usr/bin/env bash

validate_template_contract() {
    local config=$1
    local require_protection=${2:-1}
    local errors=""

    template_contract_expect() {
        local pattern=$1 message=$2
        if ! grep -qE "$pattern" <<<"$config"; then
            errors+="${message}"$'\n'
        fi
    }

    template_contract_expect "^name: ${TEMPLATE_NAME}$" "name должен быть ${TEMPLATE_NAME}"
    template_contract_expect '^template: 1$' "объект должен быть template=1"
    template_contract_expect '^ciuser: root$' "ciuser должен быть root"
    template_contract_expect '^ciupgrade: 0$' "ciupgrade должен быть 0"
    template_contract_expect '^agent: 1$' "QEMU Guest Agent должен быть включён (agent=1)"
    template_contract_expect '^vga: std$' "vga должен быть std"
    template_contract_expect '^serial0: socket$' "serial0 должен быть socket"
    template_contract_expect '^ipconfig0: ip=dhcp$' "ipconfig0 должен быть ip=dhcp"
    template_contract_expect "template-version=${TEMPLATE_VERSION}" "description должен содержать template-version=${TEMPLATE_VERSION}"

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

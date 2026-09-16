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

ensure_template() {
    if pct config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        die "VMID ${TEMPLATE_VMID} занят LXC-контейнером; создание шаблона невозможно"
    fi

    if qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        local config protection_flag
        config="$(qm config "$TEMPLATE_VMID")"
        validate_template_contract "$config" 0 \
            || die "Существующий VMID ${TEMPLATE_VMID} не соответствует полному contract шаблона ${TEMPLATE_NAME}; автоматическая миграция запрещена"

        protection_flag="$(awk -F': ' '$1=="protection" {print $2}' <<<"$config")"
        if [[ "$protection_flag" != "1" ]]; then
            qm set "$TEMPLATE_VMID" --protection 1
            config="$(qm config "$TEMPLATE_VMID")"
            validate_template_contract "$config" 1 \
                || die "После включения protection шаблон ${TEMPLATE_VMID} не прошёл полный contract check"
            ok "Для существующего шаблона ${TEMPLATE_VMID} автоматически включён protection=1"
        else
            validate_template_contract "$config" 1 \
                || die "Существующий защищённый шаблон ${TEMPLATE_VMID} не прошёл полный contract check"
            ok "Шаблон ${TEMPLATE_VMID} ${TEMPLATE_NAME} Template-Version ${TEMPLATE_VERSION} уже существует и полностью проверен"
        fi
        return
    fi

    local builder="${REPO_DIR}/scripts/pve/create-template.sh"
    [[ -f "$builder" ]] || die "Не найден канонический скрипт создания шаблона: $builder"

    log "Создание защищённого Debian-шаблона ${TEMPLATE_VMID} Template-Version ${TEMPLATE_VERSION}"
    bash "$builder"

    qm config "$TEMPLATE_VMID" >/dev/null 2>&1 \
        || die "Скрипт создания шаблона завершился, но VMID ${TEMPLATE_VMID} не создан"

    local final_config
    final_config="$(qm config "$TEMPLATE_VMID")"
    validate_template_contract "$final_config" 1 \
        || die "VMID ${TEMPLATE_VMID} создан, но не прошёл полный contract check"

    ok "Создан защищённый шаблон ${TEMPLATE_VMID} ${TEMPLATE_NAME} Template-Version ${TEMPLATE_VERSION}; полный contract проверен"
}

install_private_tooling() {
    log "Установка стабильных локальных команд PVE"

    cat >/usr/local/sbin/pve-configuration-status <<'EOF_STATUS'
#!/usr/bin/env bash
set -Eeuo pipefail
STATE=/var/lib/proxmox-deployer/state/state.json
[[ -f "$STATE" ]] || { echo "Файл состояния PVE Configuration не найден: $STATE" >&2; exit 1; }
exec jq . "$STATE"
EOF_STATUS
    chmod 0755 /usr/local/sbin/pve-configuration-status

    local deployer_src="${REPO_DIR}/scripts/pve/deploy-guest.py"
    if [[ ! -f "$deployer_src" ]]; then
        if [[ -x /usr/local/sbin/deploy-guest ]]; then
            warn "Команда /usr/local/sbin/deploy-guest существует, но её source ${deployer_src} отсутствует."
        else
            warn "Исходный файл deploy-guest пока отсутствует: ${deployer_src}"
        fi
        return
    fi

    cat >/usr/local/sbin/deploy-guest <<EOF_DEPLOY
#!/usr/bin/env bash
set -Eeuo pipefail
SOURCE="${deployer_src}"
if [[ \$EUID -eq 0 ]]; then
    exec runuser -u ${DEPLOY_USER} -- /usr/bin/python3 "\$SOURCE" "\$@"
elif [[ \$(id -un) == "${DEPLOY_USER}" ]]; then
    exec /usr/bin/python3 "\$SOURCE" "\$@"
else
    echo "deploy-guest нужно запускать от root или ${DEPLOY_USER}" >&2
    exit 1
fi
EOF_DEPLOY
    chmod 0755 /usr/local/sbin/deploy-guest
    ok "Установлена обёртка /usr/local/sbin/deploy-guest"
}

report_status() {
    local ready=1
    local deployer_src="${REPO_DIR}/scripts/pve/deploy-guest.py"

    printf '\n%s%sPVE CONFIGURATION STATUS%s\n\n' "$C_BOLD" "$C_CYAN" "$C_RESET"
    ok "Proxmox VE 9 / Debian trixie / KVM проверены"
    ok "Эксклюзивная блокировка PVE Configuration используется"
    ok "PVE/Ceph repository policy без subscription проверена"
    ok "DNS, GitHub HTTPS и доступ к PVE repository работают"
    ok "local/local-lvm готовы для images/rootdir/vztmpl/snippets"
    ok "Debian 13 LXC template подготовлен: ${LXC_TEMPLATE_VOLUME:-не определён}"
    ok "pvedeploy, config.yaml и файловая структура проверены"
    ok "PVE guest SSH identity создана/проверена"
    ok "Канонический read-only Deploy Key установлен"
    ok "Приватный репозиторий синхронизирован и origin проверен"
    ok "managed и роли Proxmox проверены"
    ok "${HOST_PVE_TOKEN}: guest-level /vms, effective permissions и API credential проверены"
    ok "${AI_PVE_TOKEN}: managed-only effective permissions и API credential проверены"

    if qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        ok "Шаблон ${TEMPLATE_VMID} Template-Version ${TEMPLATE_VERSION} существует и полный contract проверен"
    else
        printf '%s%s[НЕТ]%s Шаблон %s отсутствует\n' "$C_BOLD" "$C_RED" "$C_RESET" "$TEMPLATE_VMID"
        ready=0
    fi

    if [[ -x /usr/local/sbin/deploy-guest && -f "$deployer_src" ]]; then
        ok "Команда deploy-guest установлена и source существует"
    elif [[ -x /usr/local/sbin/deploy-guest ]]; then
        printf '%s%s[ОЖИДАНИЕ]%s Команда deploy-guest существует, но её source отсутствует\n' "$C_BOLD" "$C_YELLOW" "$C_RESET"
        ready=0
    else
        printf '%s%s[ОЖИДАНИЕ]%s deploy-guest пока не реализован\n' "$C_BOLD" "$C_YELLOW" "$C_RESET"
        ready=0
    fi

    printf '\nРевизия приватного репозитория: %s\n' "$REPO_REVISION"
    printf 'Версия PVE Configuration: %s\n' "$PVE_CONFIGURATION_VERSION"
    printf 'Предупреждений: %s\n' "$WARN_COUNT"

    if (( ready )); then
        if (( WARN_COUNT )); then
            printf '\n%s%sГОТОВО С ПРЕДУПРЕЖДЕНИЯМИ%s\n' "$C_BOLD" "$C_YELLOW" "$C_RESET"
            write_state "ready-with-warnings" "$REPO_REVISION"
        else
            printf '\n%s%sГОТОВО%s\n' "$C_BOLD" "$C_GREEN" "$C_RESET"
            write_state "ready" "$REPO_REVISION"
        fi
    else
        printf '\n%s%sЧАСТИЧНО:%s основная PVE Configuration выполнена, но один или несколько рабочих компонентов ещё не готовы.\n' "$C_BOLD" "$C_MAGENTA" "$C_RESET"
        write_state "partial" "$REPO_REVISION"
    fi
}

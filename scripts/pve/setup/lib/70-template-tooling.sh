#!/usr/bin/env bash

ensure_template() {
    if pct config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        die "VMID ${TEMPLATE_VMID} занят LXC-контейнером; создание шаблона невозможно"
    fi

    if qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        local config name template_flag protection_flag description ciuser
        config="$(qm config "$TEMPLATE_VMID")"
        name="$(awk -F': ' '$1=="name" {print $2}' <<<"$config")"
        template_flag="$(awk -F': ' '$1=="template" {print $2}' <<<"$config")"
        protection_flag="$(awk -F': ' '$1=="protection" {print $2}' <<<"$config")"
        description="$(awk -F': ' '$1=="description" {sub(/^description: /, ""); print; exit}' <<<"$config")"
        ciuser="$(awk -F': ' '$1=="ciuser" {print $2}' <<<"$config")"
        [[ "$name" == "$TEMPLATE_NAME" && "$template_flag" == "1" ]] \
            || die "VMID ${TEMPLATE_VMID} существует, но не соответствует шаблону ${TEMPLATE_NAME}"
        [[ "$description" == *"template-version=${TEMPLATE_VERSION}"* && "$ciuser" == "root" ]] \
            || die "Шаблон ${TEMPLATE_VMID} не соответствует root-only Template-Version ${TEMPLATE_VERSION}; автоматическая миграция существующего template запрещена"
        if [[ "$protection_flag" != "1" ]]; then
            qm set "$TEMPLATE_VMID" --protection 1
            protection_flag="$(qm config "$TEMPLATE_VMID" | awk -F': ' '$1=="protection" {print $2}')"
            [[ "$protection_flag" == "1" ]] \
                || die "Не удалось установить protection=1 для шаблона ${TEMPLATE_VMID} ${TEMPLATE_NAME}"
            ok "Для существующего шаблона ${TEMPLATE_VMID} автоматически включён protection=1"
        else
            ok "Шаблон ${TEMPLATE_VMID} ${TEMPLATE_NAME} Template-Version ${TEMPLATE_VERSION} уже существует и защищён"
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
    grep -q "^name: ${TEMPLATE_NAME}$" <<<"$final_config" \
        || die "VMID ${TEMPLATE_VMID} создан, но имеет неожиданное имя"
    grep -q '^template: 1$' <<<"$final_config" \
        || die "VMID ${TEMPLATE_VMID} создан, но не является шаблоном Proxmox"
    grep -q '^protection: 1$' <<<"$final_config" \
        || die "VMID ${TEMPLATE_VMID} создан, но protection=1 не установлен"
    grep -q '^ciuser: root$' <<<"$final_config" \
        || die "VMID ${TEMPLATE_VMID} создан, но ciuser не root"
    grep -q "template-version=${TEMPLATE_VERSION}" <<<"$final_config" \
        || die "VMID ${TEMPLATE_VMID} создан, но description не содержит ожидаемую Template-Version ${TEMPLATE_VERSION}"

    ok "Создан защищённый шаблон ${TEMPLATE_VMID} ${TEMPLATE_NAME} Template-Version ${TEMPLATE_VERSION}"
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

    cat >/usr/local/sbin/pve-bootstrap-status <<'EOF_LEGACY_STATUS'
#!/usr/bin/env bash
set -Eeuo pipefail
printf '[ПРЕДУПРЕЖДЕНИЕ] pve-bootstrap-status устарела; используйте pve-configuration-status\n' >&2
exec /usr/local/sbin/pve-configuration-status "$@"
EOF_LEGACY_STATUS
    chmod 0755 /usr/local/sbin/pve-bootstrap-status

    local deployer_src="${REPO_DIR}/scripts/pve/deploy-guest.py"
    if [[ ! -f "$deployer_src" ]]; then
        if [[ -x /usr/local/sbin/deploy-guest ]]; then
            warn "Исходный файл deploy-guest отсутствует, а старая wrapper-команда /usr/local/sbin/deploy-guest всё ещё существует. Она не считается готовым компонентом."
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
        ok "Шаблон ${TEMPLATE_VMID} Template-Version ${TEMPLATE_VERSION} существует и protection проверен"
    else
        printf '%s%s[НЕТ]%s Шаблон %s отсутствует\n' "$C_BOLD" "$C_RED" "$C_RESET" "$TEMPLATE_VMID"
        ready=0
    fi

    if [[ -x /usr/local/sbin/deploy-guest && -f "$deployer_src" ]]; then
        ok "Команда deploy-guest установлена и source существует"
    elif [[ -x /usr/local/sbin/deploy-guest ]]; then
        printf '%s%s[ОЖИДАНИЕ]%s Обнаружена старая deploy-guest wrapper, но её source отсутствует\n' "$C_BOLD" "$C_YELLOW" "$C_RESET"
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

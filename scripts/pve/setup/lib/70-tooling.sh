#!/usr/bin/env bash

install_private_tooling() {
    log "Установка стабильных локальных команд PVE"

    cat >/usr/local/sbin/pve-configuration-status <<'EOF_STATUS'
#!/usr/bin/env bash
set -Eeuo pipefail
STATE=/var/lib/proxmox-deployer/state/state.json
SMOKE=/var/lib/proxmox-deployer/state/template-smoke.json
[[ -f "$STATE" ]] || { echo "Файл состояния PVE Configuration не найден: $STATE" >&2; exit 1; }
printf '%s\n' '=== PVE Configuration ==='
jq . "$STATE"
if [[ -f "$SMOKE" ]]; then
    printf '\n%s\n' '=== Template Full Clone smoke ==='
    jq . "$SMOKE"
fi
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
    local ready=1 smoke_state
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

    smoke_state="$(template_smoke_state_status)"
    case "$smoke_state" in
        passed)
            ok "Full Clone smoke-test template ${TEMPLATE_VMID} подтверждён; временный VMID ${SMOKE_VMID} освобождён"
            ;;
        pending)
            printf '%s%s[ОЖИДАНИЕ]%s Full Clone smoke-test template %s имеет pending state\n' \
                "$C_BOLD" "$C_YELLOW" "$C_RESET" "$TEMPLATE_VMID"
            ready=0
            ;;
        none)
            info "Full Clone smoke state для существующего template ещё не записан; для явной проверки используйте --smoke-test-template"
            ;;
        *)
            printf '%s%s[ОШИБКА]%s Неизвестный template smoke state: %s\n' \
                "$C_BOLD" "$C_RED" "$C_RESET" "$smoke_state"
            ready=0
            ;;
    esac

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

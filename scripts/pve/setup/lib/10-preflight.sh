#!/usr/bin/env bash

check_root_and_pve() {
    [[ $EUID -eq 0 ]] || die "Запустите скрипт от root на хосте Proxmox"

    for cmd in \
        pveversion qm pct pvesh pveum pvesm pveam \
        ip systemctl apt-get getent groupadd useradd; do
        require_cmd "$cmd"
    done

    local pve_line codename
    pve_line="$(pveversion | head -n1)"
    [[ "$pve_line" =~ ^pve-manager/9\. ]] \
        || die "Для текущей инициализации ожидается Proxmox VE 9.x, обнаружено: ${pve_line:-неизвестно}"
    ok "Обнаружен Proxmox VE 9: ${pve_line}"

    codename="$(. /etc/os-release && printf '%s' "${VERSION_CODENAME:-}")"
    [[ "$codename" == "trixie" ]] \
        || die "Для текущей инициализации ожидается Debian trixie, обнаружено: '${codename:-неизвестно}'"

    [[ -e /dev/kvm ]] || die "Не найден /dev/kvm: аппаратная виртуализация/KVM недоступна"
    ok "KVM доступен"

    ip link show "$BRIDGE" >/dev/null 2>&1 || die "Не найден обязательный сетевой мост ${BRIDGE}"
    ok "Сетевой мост ${BRIDGE} найден"

    pvesm status --storage local >/dev/null 2>&1 || die "Хранилище 'local' не определено или недоступно"
    pvesm status --storage local-lvm >/dev/null 2>&1 || die "Хранилище 'local-lvm' не определено или недоступно"
    ok "Хранилища local и local-lvm найдены"

    if pct config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        die "VMID ${TEMPLATE_VMID} уже занят LXC-контейнером; скрипт не будет его изменять"
    fi

    if qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        local config protection_flag
        config="$(qm config "$TEMPLATE_VMID")"
        validate_template_contract "$config" 0 \
            || die "Шаблон ${TEMPLATE_VMID} существует, но не соответствует полному root-only Template-Version ${TEMPLATE_VERSION}. Автоматическая замена запрещена; выполните отдельную осознанную пересборку template 9000 по актуальному scripts/pve/create-template.sh."

        protection_flag="$(awk -F': ' '$1=="protection" {print $2}' <<<"$config")"
        if [[ "$protection_flag" == "1" ]]; then
            validate_template_contract "$config" 1 \
                || die "Защищённый шаблон ${TEMPLATE_VMID} не прошёл полный contract check"
            ok "Защищённый шаблон ${TEMPLATE_VMID} Template-Version ${TEMPLATE_VERSION} полностью соответствует contract"
        else
            info "Канонический шаблон ${TEMPLATE_VMID} версии ${TEMPLATE_VERSION} найден без protection=1; остальные параметры полного contract проверены, защита будет включена только после снимка конфигурации."
        fi
    fi
}

snapshot_host_config() {
    local stamp dst f rel
    stamp="$(date +%Y%m%d-%H%M%S)"
    dst="${BACKUP_ROOT}/${stamp}"

    mkdir -p "$dst/files" "$dst/diagnostics"
    chmod 0700 "$dst"

    for f in \
        /etc/network/interfaces \
        /etc/hosts \
        /etc/hostname \
        /etc/resolv.conf \
        /etc/pve/storage.cfg \
        /etc/pve/user.cfg \
        /etc/pve/datacenter.cfg; do
        if [[ -e "$f" ]]; then
            rel="${f#/}"
            mkdir -p "$dst/files/$(dirname "$rel")"
            cp -a "$f" "$dst/files/$rel"
        fi
    done

    if [[ -d /etc/apt/sources.list.d ]]; then
        mkdir -p "$dst/files/etc/apt"
        cp -a /etc/apt/sources.list "$dst/files/etc/apt/" 2>/dev/null || true
        cp -a /etc/apt/sources.list.d "$dst/files/etc/apt/" 2>/dev/null || true
    fi

    pveversion -v >"$dst/diagnostics/pveversion.txt" 2>&1 || true
    pvesm status >"$dst/diagnostics/storage-status.txt" 2>&1 || true
    pvesm status --content snippets >"$dst/diagnostics/storage-snippets.txt" 2>&1 || true
    pveam list local >"$dst/diagnostics/lxc-templates-local.txt" 2>&1 || true
    pveum user list >"$dst/diagnostics/users.txt" 2>&1 || true
    pveum role list >"$dst/diagnostics/roles.txt" 2>&1 || true
    pveum acl list >"$dst/diagnostics/acl.txt" 2>&1 || true
    pveum pool list >"$dst/diagnostics/pools.txt" 2>&1 || true
    ip addr >"$dst/diagnostics/ip-addr.txt" 2>&1 || true
    ip route >"$dst/diagnostics/ip-route.txt" 2>&1 || true
    if qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        qm config "$TEMPLATE_VMID" >"$dst/diagnostics/template-${TEMPLATE_VMID}-before.txt" 2>&1 || true
    fi

    ok "Сохранён снимок конфигурации хоста: $dst"
}

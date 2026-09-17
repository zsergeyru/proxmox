#!/usr/bin/env bash

check_node_name_resolution() {
    local node resolved local_addresses addr nonlocal=""

    node="$(hostname)"
    [[ -n "$node" ]] || die "Не удалось определить hostname PVE-ноды"

    resolved="$(getent ahosts "$node" 2>/dev/null | awk '{print $1}' | sort -u)"
    [[ -n "$resolved" ]] \
        || die "Имя PVE-ноды '${node}' не резолвится. Проверьте /etc/hosts или DNS до продолжения PVE Configuration."

    local_addresses="$(ip -o addr show up | awk '$3 == \"inet\" || $3 == \"inet6\" {sub(/\\/.*/, \"\", $4); print $4}' | sort -u)"
    [[ -n "$local_addresses" ]] \
        || die "На PVE не найдено ни одного локального IP-адреса для проверки hostname '${node}'"

    while IFS= read -r addr; do
        [[ -n "$addr" ]] || continue
        if ! grep -Fxq "$addr" <<<"$local_addresses"; then
            nonlocal+="${addr}"$'\n'
        fi
    done <<<"$resolved"

    if [[ -n "$nonlocal" ]]; then
        die "Имя PVE-ноды '${node}' резолвится в нелокальный адрес(а): $(printf '%s' "$nonlocal" | paste -sd, -). Локальные адреса: $(printf '%s\n' "$local_addresses" | paste -sd, -). Проверьте /etc/hosts или DNS."
    fi

    ok "Hostname PVE-ноды ${node} резолвится только в локальный адрес(а): $(printf '%s\n' "$resolved" | paste -sd, -)"
}

check_root_and_pve() {
    [[ $EUID -eq 0 ]] || die "Запустите скрипт от root на хосте Proxmox"

    for cmd in \
        pveversion qm pct pvesh pveum pvesm pveam \
        ip systemctl apt-get getent hostname groupadd useradd; do
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

    check_node_name_resolution

    pvesm status --storage local >/dev/null 2>&1 || die "Хранилище 'local' не определено или недоступно"
    pvesm status --storage local-lvm >/dev/null 2>&1 || die "Хранилище 'local-lvm' не определено или недоступно"
    ok "Хранилища local и local-lvm найдены"
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

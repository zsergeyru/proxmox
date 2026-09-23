#!/usr/bin/env bash

INFRA_MANAGER_VMID=910
INFRA_MANAGER_NAME="infra-manager"
INFRA_MANAGER_CORES=2
INFRA_MANAGER_MEMORY_MB=2048
INFRA_MANAGER_SWAP_MB=512
INFRA_MANAGER_DISK_GB=32
INFRA_MANAGER_DISK_STORAGE="local-lvm"
INFRA_MANAGER_BRIDGE="vmbr0"
INFRA_MANAGER_REPO="/var/lib/infra-manager/bootstrap-repo"
INFRA_MANAGER_STAGING_SECRET="/root/.infra-manager-bootstrap/pve-api.env"
INFRA_MANAGER_CREATED=0

infra_manager_config() {
    pct config "$INFRA_MANAGER_VMID" 2>/dev/null
}

validate_infra_manager_config() {
    local cfg=$1

    grep -Fxq "hostname: $INFRA_MANAGER_NAME" <<<"$cfg" \
        || die "LXC $INFRA_MANAGER_VMID имеет неверный hostname"
    grep -Fxq "cores: $INFRA_MANAGER_CORES" <<<"$cfg" \
        || die "LXC $INFRA_MANAGER_VMID: ожидается cores=$INFRA_MANAGER_CORES"
    grep -Fxq "memory: $INFRA_MANAGER_MEMORY_MB" <<<"$cfg" \
        || die "LXC $INFRA_MANAGER_VMID: ожидается memory=$INFRA_MANAGER_MEMORY_MB"
    grep -Fxq "swap: $INFRA_MANAGER_SWAP_MB" <<<"$cfg" \
        || die "LXC $INFRA_MANAGER_VMID: ожидается swap=$INFRA_MANAGER_SWAP_MB"
    grep -Fxq 'unprivileged: 1' <<<"$cfg" \
        || die "LXC $INFRA_MANAGER_VMID должен быть unprivileged"
    grep -Fxq 'onboot: 1' <<<"$cfg" \
        || die "LXC $INFRA_MANAGER_VMID должен запускаться вместе с PVE"
    grep -Fxq 'protection: 1' <<<"$cfg" \
        || die "LXC $INFRA_MANAGER_VMID должен иметь protection=1"
    grep -Eq '^features: .*keyctl=1' <<<"$cfg" \
        || die "LXC $INFRA_MANAGER_VMID должен иметь feature keyctl=1"
    grep -Eq '^features: .*nesting=1' <<<"$cfg" \
        || die "LXC $INFRA_MANAGER_VMID должен иметь feature nesting=1"
    grep -Eq "^rootfs: ${INFRA_MANAGER_DISK_STORAGE}:.*size=${INFRA_MANAGER_DISK_GB}G(,|$)" <<<"$cfg" \
        || die "LXC $INFRA_MANAGER_VMID должен иметь rootfs ${INFRA_MANAGER_DISK_STORAGE}:${INFRA_MANAGER_DISK_GB}G"
    grep -Eq "^net0: .*bridge=${INFRA_MANAGER_BRIDGE}(,|$)" <<<"$cfg" \
        || die "LXC $INFRA_MANAGER_VMID должен использовать bridge $INFRA_MANAGER_BRIDGE"
    grep -Eq '^net0: .*ip=dhcp(,|$)' <<<"$cfg" \
        || die "LXC $INFRA_MANAGER_VMID должен использовать DHCP"
}

ensure_infra_manager_lxc() {
    log "Проверка LXC 910 infra-manager"

    if qm config "$INFRA_MANAGER_VMID" >/dev/null 2>&1; then
        die "VMID $INFRA_MANAGER_VMID занят QEMU VM; автоматическое изменение запрещено"
    fi

    local cfg
    if cfg="$(infra_manager_config)"; then
        validate_infra_manager_config "$cfg"
        ok "LXC $INFRA_MANAGER_VMID уже существует и соответствует контракту"
    else
        [[ -n "$LXC_TEMPLATE_VOLUME" ]] \
            || die "Debian 13 LXC template не подготовлен"

        pct create "$INFRA_MANAGER_VMID" "$LXC_TEMPLATE_VOLUME" \
            --hostname "$INFRA_MANAGER_NAME" \
            --ostype debian \
            --cores "$INFRA_MANAGER_CORES" \
            --memory "$INFRA_MANAGER_MEMORY_MB" \
            --swap "$INFRA_MANAGER_SWAP_MB" \
            --rootfs "${INFRA_MANAGER_DISK_STORAGE}:${INFRA_MANAGER_DISK_GB}" \
            --net0 "name=eth0,bridge=${INFRA_MANAGER_BRIDGE},ip=dhcp,type=veth" \
            --unprivileged 1 \
            --features "nesting=1,keyctl=1" \
            --onboot 1 \
            --protection 1

        cfg="$(infra_manager_config)" \
            || die "pct create завершился, но LXC $INFRA_MANAGER_VMID не найден"
        validate_infra_manager_config "$cfg"
        INFRA_MANAGER_CREATED=1
        ok "Создан LXC $INFRA_MANAGER_VMID $INFRA_MANAGER_NAME"
    fi

    if [[ "$(pct status "$INFRA_MANAGER_VMID" | awk '{print $2}')" != "running" ]]; then
        pct start "$INFRA_MANAGER_VMID"
    fi

    local _
    for _ in $(seq 1 60); do
        if pct exec "$INFRA_MANAGER_VMID" -- true >/dev/null 2>&1; then
            ok "LXC $INFRA_MANAGER_VMID запущен"
            return
        fi
        sleep 1
    done

    die "LXC $INFRA_MANAGER_VMID не стал доступен через pct exec"
}

push_infra_manager_file() {
    local source=$1 target=$2 mode=$3
    pct exec "$INFRA_MANAGER_VMID" -- install -d -m 0700 "$(dirname "$target")"
    pct push "$INFRA_MANAGER_VMID" "$source" "$target" \
        --user 0 --group 0 --perms "$mode"
}

sync_infra_manager_source() (
    log "Передача текущей revision в 910"

    local archive
    archive="$(mktemp /run/infra-manager-source.XXXXXX.tar)"
    trap 'rm -f -- "$archive"' EXIT

    git -C "$SOURCE_ROOT" archive --format=tar "$RUN_SOURCE_REVISION" >"$archive" \
        || die "Не удалось сформировать Git archive текущей revision"

    pct push "$INFRA_MANAGER_VMID" "$archive" /root/infra-manager-source.tar \
        --user 0 --group 0 --perms 0600

    pct exec "$INFRA_MANAGER_VMID" -- sh -eu -c "
        rm -rf '$INFRA_MANAGER_REPO'
        install -d -m 0755 '$INFRA_MANAGER_REPO'
        tar -xf /root/infra-manager-source.tar -C '$INFRA_MANAGER_REPO'
        rm -f /root/infra-manager-source.tar
    "

    push_infra_manager_file "$KEY_FILE" /root/.ssh/github_proxmox_repo_ed25519 0600
    push_infra_manager_file "$KNOWN_HOSTS" /root/.ssh/github_known_hosts 0644

    ok "Код и read-only GitHub credential переданы в 910"
)

configure_infra_manager_access() {
    log "Настройка PVE API-доступа 910"

    local mode="apply"
    if (( INFRA_MANAGER_CREATED )); then
        mode="recover"
    fi

    INFRA_MANAGER_CTID="$INFRA_MANAGER_VMID" \
    INFRA_MANAGER_MODE="$mode" \
    INFRA_MANAGER_SECRET_FILE="$INFRA_MANAGER_STAGING_SECRET" \
        bash "$SOURCE_ROOT/scripts/infra-manager/pve-bootstrap-access.sh"

    ok "PVE API-доступ 910 подготовлен"
}

configure_infra_manager_runtime() {
    log "Настройка infra-manager"

    pct exec "$INFRA_MANAGER_VMID" -- env \
        PVE_API_SECRET_FILE="$INFRA_MANAGER_STAGING_SECRET" \
        bash "$INFRA_MANAGER_REPO/scripts/infra-manager/setup.sh"

    pct exec "$INFRA_MANAGER_VMID" -- rm -rf /root/.infra-manager-bootstrap

    ok "910 infra-manager настроен"
}

ensure_infra_manager() {
    ensure_infra_manager_lxc
    sync_infra_manager_source
    configure_infra_manager_access
    configure_infra_manager_runtime
}

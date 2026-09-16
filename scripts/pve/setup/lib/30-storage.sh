#!/usr/bin/env bash

storage_content_list() {
    local storage=$1
    pvesh get "/storage/${storage}" --output-format json \
        | jq -r '.content // empty'
}

storage_has_content() {
    local storage=$1 required=$2 content
    content="$(storage_content_list "$storage")"
    tr ',' '\n' <<<"$content" | grep -qx "$required"
}

ensure_storage_contents() {
    local storage=$1
    shift
    local content new_content required
    local -a missing=()

    content="$(storage_content_list "$storage")"
    [[ -n "$content" ]] || die "Не удалось определить content types storage ${storage}"

    for required in "$@"; do
        if ! tr ',' '\n' <<<"$content" | grep -qx "$required"; then
            missing+=("$required")
        fi
    done

    if (( ${#missing[@]} == 0 )); then
        ok "Storage ${storage} уже содержит обязательные content types: $*"
        return
    fi

    new_content="$content"
    for required in "${missing[@]}"; do
        new_content="${new_content},${required}"
    done

    pvesm set "$storage" --content "$new_content"

    for required in "$@"; do
        storage_has_content "$storage" "$required" \
            || die "После изменения storage ${storage} по-прежнему не поддерживает content type ${required}"
    done

    ok "В storage ${storage} добавлены content types без удаления существующих: ${missing[*]}"
}

storage_status_json() {
    local node
    node="$(hostname -s)"
    pvesh get "/nodes/${node}/storage" --output-format json
}

storage_is_active_enabled() {
    local storage=$1
    storage_status_json \
        | jq -e --arg storage "$storage" '
            .[]
            | select(.storage == $storage)
            | select((.enabled // 1) == 1 or (.enabled // 1) == true)
            | select(.active == 1 or .active == true)
        ' >/dev/null
}

ensure_storage_layout() {
    log "Проверка активности и content types базовых storage"

    storage_is_active_enabled local \
        || die "Storage local не активен или отключён на этом PVE node"
    storage_is_active_enabled local-lvm \
        || die "Storage local-lvm не активен или отключён на этом PVE node"

    ensure_storage_contents local snippets vztmpl
    ensure_storage_contents local-lvm images rootdir

    ok "Storage local/local-lvm активны и готовы для VM, LXC, templates и snippets"
}

ensure_lxc_template() {
    log "Проверка Debian 13 LXC template"

    pveam update || die "Не удалось обновить каталог Proxmox LXC templates через pveam update"

    local template
    template="$(pveam available --section system \
        | awk '$1=="system" && $2 ~ /^debian-13-standard_.*_amd64\.tar\.(zst|xz|gz)$/ {print $2}' \
        | sort -V \
        | tail -n1)"

    [[ -n "$template" ]] \
        || die "В каталоге pveam не найден Debian 13 standard LXC template"

    LXC_TEMPLATE_VOLUME="${LXC_TEMPLATE_STORAGE}:vztmpl/${template}"

    if pveam list "$LXC_TEMPLATE_STORAGE" \
        | awk 'NR>1 {print $1}' \
        | grep -Fxq "$LXC_TEMPLATE_VOLUME"; then
        ok "Актуальный Debian 13 LXC template уже загружен: ${LXC_TEMPLATE_VOLUME}"
        return
    fi

    pveam download "$LXC_TEMPLATE_STORAGE" "$template" \
        || die "Не удалось скачать Debian 13 LXC template ${template} в storage ${LXC_TEMPLATE_STORAGE}"

    pveam list "$LXC_TEMPLATE_STORAGE" \
        | awk 'NR>1 {print $1}' \
        | grep -Fxq "$LXC_TEMPLATE_VOLUME" \
        || die "pveam download завершился, но ${LXC_TEMPLATE_VOLUME} не найден в storage"

    ok "Debian 13 LXC template загружен: ${LXC_TEMPLATE_VOLUME}"
}

storage_available_bytes() {
    local storage=$1
    storage_status_json \
        | jq -r --arg storage "$storage" '.[] | select(.storage == $storage) | (.avail // empty)' \
        | head -n1
}

check_template_capacity() {
    if qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        return
    fi

    log "Проверка свободного места для создания Debian-шаблона"

    local local_avail disk_avail
    local_avail="$(storage_available_bytes local)"
    disk_avail="$(storage_available_bytes local-lvm)"

    [[ "$local_avail" =~ ^[0-9]+$ ]] \
        || die "Не удалось определить доступное место на storage local"
    [[ "$disk_avail" =~ ^[0-9]+$ ]] \
        || die "Не удалось определить доступное место на storage local-lvm"

    (( local_avail >= TEMPLATE_MIN_LOCAL_BYTES )) \
        || die "Недостаточно свободного места на local для образа Debian: доступно $((local_avail / 1024 / 1024)) MiB, требуется минимум $((TEMPLATE_MIN_LOCAL_BYTES / 1024 / 1024)) MiB"
    (( disk_avail >= TEMPLATE_MIN_DISK_STORAGE_BYTES )) \
        || die "Недостаточно свободного места на local-lvm для template disk: доступно $((disk_avail / 1024 / 1024 / 1024)) GiB, требуется минимум $((TEMPLATE_MIN_DISK_STORAGE_BYTES / 1024 / 1024 / 1024)) GiB"

    ok "Свободного места для создания template достаточно"
}

check_template_source() {
    if qm config "$TEMPLATE_VMID" >/dev/null 2>&1; then
        return
    fi

    log "Проверка источника облачного образа Debian"
    getent ahosts cloud.debian.org >/dev/null || die "Не работает DNS-разрешение имени cloud.debian.org"
    curl -fsS --connect-timeout 10 -o /dev/null \
        https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS \
        || die "Нет HTTPS-доступа к облачным образам Debian"
    ok "Источник облачного образа Debian доступен"
}

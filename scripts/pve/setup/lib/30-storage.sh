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

local_debian13_lxc_volumes() {
    pveam list "$LXC_TEMPLATE_STORAGE" 2>/dev/null \
        | awk 'NR>1 {print $1}' \
        | grep -E "^${LXC_TEMPLATE_STORAGE}:vztmpl/debian-13-standard_.*_amd64\\.tar\\.(zst|xz|gz)$" \
        | sort -V || true
}

newest_local_debian13_lxc_volume() {
    local_debian13_lxc_volumes | tail -n1
}

cleanup_old_debian13_lxc_templates() {
    local keep=$1 volume
    while IFS= read -r volume; do
        [[ -n "$volume" && "$volume" != "$keep" ]] || continue
        if pveam remove "$volume"; then
            ok "Удалён устаревший Debian 13 LXC template cache: ${volume}"
        else
            warn "Не удалось удалить устаревший Debian 13 LXC template cache ${volume}; рабочий template ${keep} уже готов"
        fi
    done < <(local_debian13_lxc_volumes)
}

ensure_lxc_template() {
    log "Проверка Debian 13 LXC template"

    local local_volume template catalog_updated=0
    local_volume="$(newest_local_debian13_lxc_volume)"

    if pveam update; then
        catalog_updated=1
    elif [[ -n "$local_volume" ]]; then
        warn "pveam update временно недоступен; используется уже загруженный Debian 13 LXC template ${local_volume}"
        LXC_TEMPLATE_VOLUME="$local_volume"
        return
    else
        die "Не удалось обновить каталог Proxmox LXC templates, а локальный Debian 13 template отсутствует"
    fi

    template="$(pveam available --section system 2>/dev/null \
        | awk '$1=="system" && $2 ~ /^debian-13-standard_.*_amd64\.tar\.(zst|xz|gz)$/ {print $2}' \
        | sort -V \
        | tail -n1)"

    if [[ -z "$template" ]]; then
        if [[ -n "$local_volume" ]]; then
            warn "Свежий каталог pveam не содержит Debian 13 standard; сохраняется локальный ${local_volume}"
            LXC_TEMPLATE_VOLUME="$local_volume"
            return
        fi
        die "В каталоге pveam не найден Debian 13 standard LXC template"
    fi

    LXC_TEMPLATE_VOLUME="${LXC_TEMPLATE_STORAGE}:vztmpl/${template}"

    if pveam list "$LXC_TEMPLATE_STORAGE" \
        | awk 'NR>1 {print $1}' \
        | grep -Fxq "$LXC_TEMPLATE_VOLUME"; then
        ok "Актуальный Debian 13 LXC template уже загружен: ${LXC_TEMPLATE_VOLUME}"
    else
        pveam download "$LXC_TEMPLATE_STORAGE" "$template" \
            || die "Не удалось скачать Debian 13 LXC template ${template} в storage ${LXC_TEMPLATE_STORAGE}"

        pveam list "$LXC_TEMPLATE_STORAGE" \
            | awk 'NR>1 {print $1}' \
            | grep -Fxq "$LXC_TEMPLATE_VOLUME" \
            || die "pveam download завершился, но ${LXC_TEMPLATE_VOLUME} не найден в storage"

        ok "Debian 13 LXC template загружен: ${LXC_TEMPLATE_VOLUME}"
    fi

    if (( catalog_updated )); then
        cleanup_old_debian13_lxc_templates "$LXC_TEMPLATE_VOLUME"
    fi
}

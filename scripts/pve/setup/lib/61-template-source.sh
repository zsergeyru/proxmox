#!/usr/bin/env bash

storage_available_bytes() {
    local storage=$1
    storage_status_json \
        | jq -r --arg storage "$storage" '.[] | select(.storage == $storage) | (.avail // empty)' \
        | head -n1
}

check_template_capacity() {
    (( TEMPLATE_BUILD_REQUIRED == 1 )) || return 0

    log "Проверка свободного места для создания Debian template"

    local local_avail disk_avail
    local_avail="$(storage_available_bytes "$TEMPLATE_SNIPPET_STORAGE")"
    disk_avail="$(storage_available_bytes "$TEMPLATE_DISK_STORAGE")"

    [[ "$local_avail" =~ ^[0-9]+$ ]] \
        || die "Не удалось определить доступное место на storage ${TEMPLATE_SNIPPET_STORAGE}"
    [[ "$disk_avail" =~ ^[0-9]+$ ]] \
        || die "Не удалось определить доступное место на storage ${TEMPLATE_DISK_STORAGE}"

    (( local_avail >= TEMPLATE_MIN_LOCAL_BYTES )) \
        || die "Недостаточно свободного места на ${TEMPLATE_SNIPPET_STORAGE}: доступно $((local_avail / 1024 / 1024)) MiB, требуется минимум $((TEMPLATE_MIN_LOCAL_BYTES / 1024 / 1024)) MiB"
    (( disk_avail >= TEMPLATE_MIN_DISK_STORAGE_BYTES )) \
        || die "Недостаточно свободного места на ${TEMPLATE_DISK_STORAGE}: доступно $((disk_avail / 1024 / 1024 / 1024)) GiB, требуется минимум $((TEMPLATE_MIN_DISK_STORAGE_BYTES / 1024 / 1024 / 1024)) GiB"

    ok "Свободного места для создания template достаточно"
}

check_template_source() {
    (( TEMPLATE_BUILD_REQUIRED == 1 )) || return 0

    log "Проверка источника облачного образа Debian"
    getent ahosts cloud.debian.org >/dev/null || die "Не работает DNS-разрешение имени cloud.debian.org"
    curl -fsS --connect-timeout 10 --max-time 30 -o /dev/null "$TEMPLATE_CHECKSUM_URL" \
        || die "Нет HTTPS-доступа к облачным образам Debian"
    ok "Источник облачного образа Debian доступен"
}

download_template_file() {
    local url=$1 dst=$2 dst_dir dst_name
    dst_dir="$(dirname "$dst")"
    dst_name="$(basename "$dst")"
    mkdir -p "$dst_dir"

    # Общая orchestration lock гарантирует отсутствие параллельной загрузки. Поэтому
    # временные файлы того же назначения являются остатками прерванных запусков.
    find "$dst_dir" -maxdepth 1 -type f -name ".${dst_name}.tmp.*" -delete \
        || die "Не удалось удалить stale temporary downloads для ${dst}"

    TEMPLATE_DOWNLOAD_TMP="$(mktemp "${dst_dir}/.${dst_name}.tmp.XXXXXX")"
    if ! curl \
        --fail \
        --location \
        --retry 3 \
        --retry-delay 2 \
        --retry-all-errors \
        --connect-timeout 15 \
        --speed-limit 1024 \
        --speed-time 60 \
        --output "$TEMPLATE_DOWNLOAD_TMP" \
        "$url"; then
        cleanup_template_download_tmp
        die "Не удалось скачать ${url}"
    fi

    mv -f -- "$TEMPLATE_DOWNLOAD_TMP" "$dst"
    TEMPLATE_DOWNLOAD_TMP=""
}

prepare_template_image() {
    (( TEMPLATE_BUILD_REQUIRED == 1 )) || return 0

    log "Загрузка и проверка стандартного cloud-образа Debian 13"
    mkdir -p "$TEMPLATE_IMAGE_DIR"

    download_template_file "$TEMPLATE_CHECKSUM_URL" "$TEMPLATE_CHECKSUM_PATH"
    download_template_file "$TEMPLATE_IMAGE_URL" "$TEMPLATE_IMAGE_PATH"

    local checksum_line
    checksum_line="$(awk -v f="$TEMPLATE_IMAGE_NAME" '$2 == f || $2 == ("*" f) {print; exit}' "$TEMPLATE_CHECKSUM_PATH")"
    [[ -n "$checksum_line" ]] \
        || die "Для ${TEMPLATE_IMAGE_NAME} не найдена контрольная сумма в ${TEMPLATE_CHECKSUM_URL}"

    TEMPLATE_IMAGE_SHA512="$(awk '{print $1}' <<<"$checksum_line")"
    [[ "$TEMPLATE_IMAGE_SHA512" =~ ^[0-9a-fA-F]{128}$ ]] \
        || die "Некорректная контрольная сумма SHA-512 для ${TEMPLATE_IMAGE_NAME}"

    (
        cd "$TEMPLATE_IMAGE_DIR"
        printf '%s\n' "$checksum_line" | sha512sum --check --strict -
    ) || die "SHA-512 cloud-образа Debian не совпадает с опубликованной контрольной суммой"

    ok "Cloud-образ Debian проверен: ${TEMPLATE_IMAGE_NAME}"
}

prepare_template_cloud_init() {
    (( TEMPLATE_BUILD_REQUIRED == 1 )) || return 0

    log "Сборка временного Cloud-Init из versioned template assets"

    local base_yaml bootstrap_asset finalize_asset
    base_yaml="${TEMPLATE_ASSET_DIR}/cloud-init.yaml"
    bootstrap_asset="${TEMPLATE_ASSET_DIR}/template-bootstrap.sh"
    finalize_asset="${TEMPLATE_ASSET_DIR}/template-finalize.sh"

    [[ -f "$base_yaml" ]] || die "Не найден template asset: ${base_yaml}"
    [[ -f "$bootstrap_asset" ]] || die "Не найден template asset: ${bootstrap_asset}"
    [[ -f "$finalize_asset" ]] || die "Не найден template asset: ${finalize_asset}"
    [[ -f "$TEMPLATE_RENDERER" ]] || die "Не найден canonical Cloud-Init renderer: ${TEMPLATE_RENDERER}"
    [[ -n "$TEMPLATE_IMAGE_SHA512" ]] || die "SHA-512 исходного образа не определён до сборки Cloud-Init"

    pvesm status --storage "$TEMPLATE_DISK_STORAGE" >/dev/null 2>&1 \
        || die "Хранилище '${TEMPLATE_DISK_STORAGE}' недоступно"
    pvesm status --storage "$TEMPLATE_SNIPPET_STORAGE" >/dev/null 2>&1 \
        || die "Хранилище snippets '${TEMPLATE_SNIPPET_STORAGE}' недоступно"

    if ! TEMPLATE_SNIPPET_PATH="$(pvesm path "$TEMPLATE_SNIPPET_VOL" 2>/dev/null)"; then
        die "Для storage '${TEMPLATE_SNIPPET_STORAGE}' не включён content type Snippets"
    fi

    # При TEMPLATE_BUILD_REQUIRED=1 VMID уже подтверждён как свободный. Поэтому
    # одноимённый snippet без builder VM является безопасным orphaned build artifact.
    if [[ -e "$TEMPLATE_SNIPPET_PATH" ]]; then
        rm -f -- "$TEMPLATE_SNIPPET_PATH"
        info "Удалён orphaned temporary snippet предыдущей попытки: ${TEMPLATE_SNIPPET_PATH}"
    fi
    mkdir -p "$(dirname "$TEMPLATE_SNIPPET_PATH")"

    python3 "$TEMPLATE_RENDERER" \
        --base "$base_yaml" \
        --bootstrap "$bootstrap_asset" \
        --finalize "$finalize_asset" \
        --output "$TEMPLATE_SNIPPET_PATH" \
        --template-version "$TEMPLATE_VERSION" \
        --image-name "$TEMPLATE_IMAGE_NAME" \
        --image-sha512 "$TEMPLATE_IMAGE_SHA512" \
        || die "Canonical renderer не смог сформировать Cloud-Init builder snippet"

    [[ -s "$TEMPLATE_SNIPPET_PATH" ]] \
        || die "Canonical renderer завершился без создания непустого ${TEMPLATE_SNIPPET_PATH}"

    ok "Cloud-Init builder snippet подготовлен canonical renderer: ${TEMPLATE_SNIPPET_VOL}"
}

prepare_template_source() {
    (( TEMPLATE_BUILD_REQUIRED == 1 )) || return 0
    check_template_capacity
    check_template_source
    prepare_template_image
    prepare_template_cloud_init
}

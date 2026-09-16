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
    curl -fsS --connect-timeout 10 -o /dev/null "$TEMPLATE_CHECKSUM_URL" \
        || die "Нет HTTPS-доступа к облачным образам Debian"
    ok "Источник облачного образа Debian доступен"
}

download_template_file() {
    local url=$1 dst=$2 tmp
    tmp="${dst}.tmp.$$"
    rm -f -- "$tmp"
    if ! curl --fail --location --retry 3 --connect-timeout 15 --output "$tmp" "$url"; then
        rm -f -- "$tmp"
        die "Не удалось скачать ${url}"
    fi
    mv -f -- "$tmp" "$dst"
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

    python3 - \
        "$base_yaml" "$bootstrap_asset" "$finalize_asset" "$TEMPLATE_SNIPPET_PATH" \
        "$TEMPLATE_VERSION" "$TEMPLATE_IMAGE_NAME" "$TEMPLATE_IMAGE_SHA512" <<'PY_TEMPLATE_CLOUD_INIT'
from pathlib import Path
import sys
import yaml

base_path, bootstrap_path, finalize_path, output_path = map(Path, sys.argv[1:5])
template_version, image_name, image_sha512 = sys.argv[5:8]

data = yaml.safe_load(base_path.read_text(encoding="utf-8"))
if not isinstance(data, dict):
    raise SystemExit(f"{base_path} must contain a YAML mapping")

bootstrap = bootstrap_path.read_text(encoding="utf-8")
bootstrap = (
    bootstrap.replace("__TEMPLATE_VERSION__", template_version)
    .replace("__IMAGE_NAME__", image_name)
    .replace("__IMAGE_SHA512__", image_sha512)
)
finalize = finalize_path.read_text(encoding="utf-8")

replacement = {
    "/usr/local/sbin/template-bootstrap": bootstrap,
    "/usr/local/sbin/template-finalize": finalize,
}
found = set()
for item in data.get("write_files", []):
    if not isinstance(item, dict):
        continue
    path = item.get("path")
    if path in replacement:
        item["content"] = replacement[path]
        found.add(path)

missing = set(replacement) - found
if missing:
    raise SystemExit(f"Cloud-Init asset lacks write_files entries: {sorted(missing)}")

rendered = "#cloud-config\n" + yaml.safe_dump(
    data,
    allow_unicode=True,
    sort_keys=False,
    default_flow_style=False,
)
Path(output_path).write_text(rendered, encoding="utf-8")
PY_TEMPLATE_CLOUD_INIT

    python3 - "$TEMPLATE_SNIPPET_PATH" <<'PY_VALIDATE_RENDERED_CLOUD_INIT'
from pathlib import Path
import sys
import yaml

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
data = yaml.safe_load(text)
if not isinstance(data, dict):
    raise SystemExit("Rendered Cloud-Init is not a mapping")
for marker in ("__TEMPLATE_BOOTSTRAP_CONTENT__", "__TEMPLATE_FINALIZE_CONTENT__", "__TEMPLATE_VERSION__", "__IMAGE_NAME__", "__IMAGE_SHA512__"):
    if marker in text:
        raise SystemExit(f"Unresolved template marker: {marker}")
PY_VALIDATE_RENDERED_CLOUD_INIT

    ok "Cloud-Init builder snippet подготовлен: ${TEMPLATE_SNIPPET_VOL}"
}

prepare_template_source() {
    (( TEMPLATE_BUILD_REQUIRED == 1 )) || return 0
    check_template_capacity
    check_template_source
    prepare_template_image
    prepare_template_cloud_init
}

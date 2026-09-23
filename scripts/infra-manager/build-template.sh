#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CA_BUNDLE="/etc/infra-manager/ca/ca-bundle.crt"
NODE="${PACKER_NODE:-pve}"
ISO_BASE="https://cdimage.debian.org/debian-cd/current/amd64/iso-cd"

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ОК] %s\n' "$*"; }
info() { printf '==> %s\n' "$*"; }

usage() {
    cat <<'USAGE'
Использование:
  build-template.sh 9000

Собирает шаблон Packer из каталога packer/<VMID>.
Сейчас поддерживается шаблон 9000 (Debian 13).
USAGE
}

[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { usage; exit 0; }
[[ $# -eq 1 ]] || { usage >&2; exit 2; }

VMID=$1
[[ "$VMID" =~ ^[0-9]+$ ]] || die "VMID должен быть числом"
[[ "$VMID" == "9000" ]] || die "Пока поддерживается только шаблон 9000"

PACKER_DIR="$ROOT/packer/$VMID"
[[ -d "$PACKER_DIR" ]] || die "Не найден каталог $PACKER_DIR"
[[ -s "$CA_BUNDLE" ]] || die "Не найден CA bundle: $CA_BUNDLE"

# Packer и его Proxmox-плагин используют системный TLS стек Go.
# Передаём им подготовленный bundle с PVE CA, не отключая проверку сертификата.
export SSL_CERT_FILE="$CA_BUNDLE"

for command in curl jq openssl packer; do
    command -v "$command" >/dev/null 2>&1 || die "Не найден $command"
done

: "${TF_VAR_pve_endpoint:?TF_VAR_pve_endpoint не задан}"
: "${TF_VAR_pve_api_token:?TF_VAR_pve_api_token не задан}"

PVE_ENDPOINT="${TF_VAR_pve_endpoint%/}"
PVE_API="${PVE_ENDPOINT}/api2/json"
PVE_TOKEN_ID="${TF_VAR_pve_api_token%%=*}"
PVE_TOKEN_SECRET="${TF_VAR_pve_api_token#*=}"

[[ -n "$PVE_TOKEN_ID" && -n "$PVE_TOKEN_SECRET" && "$PVE_TOKEN_ID" != "$PVE_TOKEN_SECRET" ]] \
    || die "TF_VAR_pve_api_token имеет неверный формат"

AUTH_HEADER="Authorization: PVEAPIToken=${PVE_TOKEN_ID}=${PVE_TOKEN_SECRET}"

api() {
    local method=$1 endpoint=$2
    shift 2
    curl -fsS \
        --connect-timeout 5 \
        --max-time 120 \
        --cacert "$CA_BUNDLE" \
        -H "$AUTH_HEADER" \
        -X "$method" \
        "$@" \
        "${PVE_API}${endpoint}"
}

template_config() {
    api GET "/nodes/${NODE}/qemu/${VMID}/config" | jq -c '.data'
}

check_existing() {
    local resource config name description template
    resource="$(api GET "/cluster/resources" \
        --get --data-urlencode "type=vm" \
        | jq -c --argjson vmid "$VMID" '.data[]? | select(.vmid == $vmid)' | head -n1)"

    [[ -n "$resource" ]] || return 1

    [[ "$(jq -r '.type // empty' <<<"$resource")" == "qemu" ]] \
        || die "VMID $VMID уже занят LXC или другим объектом"

    config="$(template_config)" || die "Не удалось прочитать VMID $VMID"
    template="$(jq -r '.template // 0' <<<"$config")"
    name="$(jq -r '.name // empty' <<<"$config")"
    description="$(jq -r '.description // empty' <<<"$config")"

    if [[ "$template" == "1" && "$name" == "tpl-debian13" && "$description" == *"template-version=8"* ]]; then
        finalize_template
        "$ROOT/scripts/infra-manager/test-template.sh" "$VMID"
        ok "Шаблон $VMID уже соответствует версии 8 и успешно проверен; сборка не требуется"
        return 0
    fi

    if [[ "$name" == "builder-$VMID" ]]; then
        die "VMID $VMID содержит незавершённую VM-сборщик. Она оставлена для диагностики."
    fi

    die "VMID $VMID уже занят несовместимым объектом. Автоматическая замена запрещена."
}

resolve_debian_iso() {
    local sums line checksum filename
    info "Определение актуального Debian 13 netinst ISO"
    sums="$(curl -fsSL --connect-timeout 10 --max-time 60 "${ISO_BASE}/SHA512SUMS")" \
        || die "Не удалось получить SHA512SUMS Debian"

    line="$(awk '$2 ~ /^debian-13([.][0-9]+)+-amd64-netinst[.]iso$/ {print $1 " " $2}' <<<"$sums" | tail -n1)"
    checksum="${line%% *}"
    filename="${line#* }"

    [[ "$checksum" =~ ^[0-9a-fA-F]{128}$ && "$filename" != "$line" ]] \
        || die "В current Debian не найден Debian 13 amd64 netinst ISO"

    ISO_URL="${ISO_BASE}/${filename}"
    ISO_CHECKSUM="sha512:${checksum}"
    info "Источник: $filename"
}

verify_template() {
    local config failures
    config="$(template_config)"

    failures="$(jq -r '
        [
            (if (((.template // 0) | tostring) == "1" or ((.template // false) | tostring) == "true")
             then empty else "template=" + ((.template // "<отсутствует>") | tostring) end),
            (if .name == "tpl-debian13"
             then empty else "name=" + ((.name // "<отсутствует>") | tostring) end),
            (if ((.description // "") | contains("template-version=8"))
             then empty else "description=" + ((.description // "<отсутствует>") | tostring) end),
            (if (((.protection // 0) | tostring) == "1" or ((.protection // false) | tostring) == "true")
             then empty else "protection=" + ((.protection // "<отсутствует>") | tostring) end),
            (if .scsihw == "virtio-scsi-single"
             then empty else "scsihw=" + ((.scsihw // "<отсутствует>") | tostring) end),
            (
                ((.agent // "") | tostring) as $agent
                | if ($agent == "1" or ($agent | startswith("1,")) or ($agent | test("(^|,)enabled=1($|,)")))
                  then empty else "agent=" + (if $agent == "" then "<отсутствует>" else $agent end) end
            ),
            (if ([
                    to_entries[]
                    | select(.key | test("^(ide|sata|scsi|virtio)[0-9]+$"))
                    | select((.value | tostring) | contains("cloudinit"))
                ] | length) > 0
             then empty else "cloudinit-disk=<отсутствует>" end),
            (if .ciuser == "root"
             then empty else "ciuser=" + ((.ciuser // "<отсутствует>") | tostring) end),
            (if ((.ipconfig0 // "") | contains("ip=dhcp"))
             then empty else "ipconfig0=" + ((.ipconfig0 // "<отсутствует>") | tostring) end),
            (
                ((.ciupgrade // 0) | tostring) as $ciupgrade
                | if ($ciupgrade == "0" or $ciupgrade == "false")
                  then empty else "ciupgrade=" + $ciupgrade end
            )
        ]
        | .[]
    ' <<<"$config")"

    if [[ -n "$failures" ]]; then
        printf 'ОШИБКА: Шаблон %s не соответствует базовому контракту:\n' "$VMID" >&2
        while IFS= read -r failure; do
            [[ -n "$failure" ]] && printf '  - %s\n' "$failure" >&2
        done <<<"$failures"
        printf 'Фактическая конфигурация: %s\n' \
            "$(jq -c '{template,name,description,protection,scsihw,agent,drives:(with_entries(select(.key | test("^(ide|sata|scsi|virtio)[0-9]+$")))),ciuser,ipconfig0,ciupgrade}' <<<"$config")" >&2
        exit 1
    fi

    ok "Контракт шаблона $VMID подтверждён"
}

finalize_template() {
    info "Финальная конфигурация Cloud-Init и защиты"
    api PUT "/nodes/${NODE}/qemu/${VMID}/config" \
        --data-urlencode "ciuser=root" \
        --data-urlencode "ciupgrade=0" \
        --data-urlencode "ipconfig0=ip=dhcp" \
        --data-urlencode "protection=1" >/dev/null
    verify_template
}

main() {
    if check_existing; then
        exit 0
    fi

    resolve_debian_iso

    local build_password
    build_password="$(openssl rand -hex 24)"

    info "Проверка Packer"
    packer init "$PACKER_DIR"
    packer validate \
        -var="proxmox_url=$PVE_API" \
        -var="proxmox_username=$PVE_TOKEN_ID" \
        -var="proxmox_token=$PVE_TOKEN_SECRET" \
        -var="node=$NODE" \
        -var="iso_url=$ISO_URL" \
        -var="iso_checksum=$ISO_CHECKSUM" \
        -var="build_password=$build_password" \
        "$PACKER_DIR"

    info "Сборка шаблона $VMID"
    packer build \
        -var="proxmox_url=$PVE_API" \
        -var="proxmox_username=$PVE_TOKEN_ID" \
        -var="proxmox_token=$PVE_TOKEN_SECRET" \
        -var="node=$NODE" \
        -var="iso_url=$ISO_URL" \
        -var="iso_checksum=$ISO_CHECKSUM" \
        -var="build_password=$build_password" \
        "$PACKER_DIR"

    unset build_password

    finalize_template
    "$ROOT/scripts/infra-manager/test-template.sh" "$VMID"

    unset PVE_TOKEN_SECRET AUTH_HEADER
    ok "Шаблон $VMID полностью собран и проверен"
}

main

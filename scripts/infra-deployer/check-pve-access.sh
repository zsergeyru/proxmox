#!/usr/bin/env bash
set -Eeuo pipefail

PVE_ENV="/etc/infra-deployer/secrets/pve-api.env"
CA_BUNDLE="/etc/infra-deployer/ca/ca-bundle.crt"

MANAGED_POOL="managed"

VM_ADMIN_PRIVS="VM.Allocate VM.Audit VM.Config.CDROM VM.Config.Cloudinit VM.Config.CPU VM.Config.Disk VM.Config.HWType VM.Config.Memory VM.Config.Network VM.Config.Options VM.GuestAgent.Audit VM.PowerMgmt"
FORBIDDEN_ROOT_PRIVS="Permissions.Modify Sys.Modify Sys.PowerMgmt User.Modify Group.Allocate Realm.Allocate Realm.AllocateUser Pool.Allocate Datastore.Allocate Datastore.AllocateSpace Datastore.AllocateTemplate SDN.Allocate SDN.Use Mapping.Modify"

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ОК] %s\n' "$*"; }

read_env_value() {
    local key=$1
    sed -n "s/^${key}=//p" "$PVE_ENV" | head -n1
}

priv_lines() {
    printf '%s\n' "$1" | tr ', ' '\n\n' | sed '/^$/d' | LC_ALL=C sort -u
}

[[ -s "$PVE_ENV" ]] || die "Не найден PVE credential: $PVE_ENV"
[[ -s "$CA_BUNDLE" ]] || die "Не найден CA bundle: $CA_BUNDLE"
command -v curl >/dev/null 2>&1 || die "Не найден curl"
command -v jq >/dev/null 2>&1 || die "Не найден jq"

PVE_API_URL="$(read_env_value PVE_API_URL)"
PVE_API_TOKEN_ID="$(read_env_value PVE_API_TOKEN_ID)"
PVE_API_TOKEN_SECRET="$(read_env_value PVE_API_TOKEN_SECRET)"

[[ -n "$PVE_API_URL" ]] || die "PVE_API_URL пуст"
[[ -n "$PVE_API_TOKEN_ID" ]] || die "PVE_API_TOKEN_ID пуст"
[[ -n "$PVE_API_TOKEN_SECRET" ]] || die "PVE_API_TOKEN_SECRET пуст"

AUTH_HEADER="Authorization: PVEAPIToken=${PVE_API_TOKEN_ID}=${PVE_API_TOKEN_SECRET}"

api_get() {
    local endpoint=$1
    shift
    curl -fsS \
        --connect-timeout 5 \
        --max-time 20 \
        --cacert "$CA_BUNDLE" \
        -H "$AUTH_HEADER" \
        "$@" \
        "${PVE_API_URL}/api2/json${endpoint}"
}

permissions_at() {
    local path=$1
    api_get "/access/permissions" \
        --get \
        --data-urlencode "path=$path" \
        | jq -c '.data'
}

permission_present() {
    local json=$1 privilege=$2
    jq -e --arg privilege "$privilege" '
        any(.[]?;
            (type == "object")
            and (
                ((.[$privilege] // 0) == 1)
                or ((.[$privilege] // false) == true)
            )
        )
    ' <<<"$json" >/dev/null
}

require_permissions() {
    local path=$1 raw=$2 json privilege missing=""
    json="$(permissions_at "$path")" \
        || die "Не удалось получить permissions для $path"

    while IFS= read -r privilege; do
        [[ -n "$privilege" ]] || continue
        permission_present "$json" "$privilege" \
            || missing="$missing $privilege"
    done < <(priv_lines "$raw")

    [[ -z "$missing" ]] \
        || die "На $path отсутствуют обязательные privileges:$missing"
}

forbid_permissions() {
    local path=$1 raw=$2 json privilege found=""
    json="$(permissions_at "$path")" \
        || die "Не удалось получить permissions для $path"

    while IFS= read -r privilege; do
        [[ -n "$privilege" ]] || continue
        permission_present "$json" "$privilege" \
            && found="$found $privilege"
    done < <(priv_lines "$raw")

    [[ -z "$found" ]] \
        || die "На $path обнаружены запрещённые privileges:$found"
}


api_get "/version" | jq -e '.data.version // .data.release' >/dev/null \
    || die "PVE API token не прошёл проверку авторизации"

api_get "/pools/$MANAGED_POOL" | jq -e '.data' >/dev/null \
    || die "Pool $MANAGED_POOL недоступен"

require_permissions "/vms" "$VM_ADMIN_PRIVS"
require_permissions "/storage/local" "Datastore.Audit"
require_permissions "/storage/local-lvm" "Datastore.Audit Datastore.AllocateSpace"
require_permissions "/sdn/zones/localnetwork/vmbr0" "SDN.Audit SDN.Use"

forbid_permissions "/" "$FORBIDDEN_ROOT_PRIVS"

unset PVE_API_TOKEN_SECRET AUTH_HEADER
ok "PVE API access infra-deployer соответствует контракту"

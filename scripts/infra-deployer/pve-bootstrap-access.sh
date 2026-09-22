#!/usr/bin/env bash
set -Eeuo pipefail

# Этот сценарий хранится в закрытом проекте, но выполняется от root на PVE.
# Он содержит всю PVE-политику, необходимую 910. Public bootstrap только
# временно забирает этот файл из 910 и запускает его.

CTID="${INFRA_DEPLOYER_CTID:-910}"
MODE="${INFRA_DEPLOYER_MODE:-apply}"
CT_SECRET_FILE="${INFRA_DEPLOYER_SECRET_FILE:-/root/.infra-deployer-bootstrap/pve-api.env}"
CT_PERSISTENT_SECRET="${INFRA_DEPLOYER_PERSISTENT_SECRET:-/etc/infra-deployer/secrets/pve-api.env}"

API_USER="root@pam"
API_TOKEN_NAME="infra-deployer"
API_TOKEN_ID="${API_USER}!${API_TOKEN_NAME}"

MANAGED_POOL="managed"
CT_STORAGE="local-lvm"
CT_BRIDGE="vmbr0"
LEGACY_TEMPLATE_VMID=9000

LEGACY_API_USER="infra-deployer@pve"
LEGACY_API_TOKEN_NAME="automation"
LEGACY_API_TOKEN_ID="${LEGACY_API_USER}!${LEGACY_API_TOKEN_NAME}"
LEGACY_ROLE="InfraManagedGuest"

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ОК] %s\n' "$*"; }

require_pve_root() {
    [[ $EUID -eq 0 ]] || die "Сценарий должен выполняться от root на PVE"
    command -v pveum >/dev/null 2>&1 || die "Не найден pveum"
    command -v pct >/dev/null 2>&1 || die "Не найден pct"
    command -v jq >/dev/null 2>&1 || die "Не найден jq"
    pct status "$CTID" >/dev/null 2>&1 || die "LXC $CTID отсутствует"
}

ct_exec() {
    pct exec "$CTID" -- "$@"
}

managed_pool_exists() {
    pvesh get "/pools/$MANAGED_POOL" --output-format json >/dev/null 2>&1
}

ensure_managed_pool() {
    managed_pool_exists && return
    pveum pool add "$MANAGED_POOL" --comment "Guests managed from 910 infra-deployer"
    ok "Создан pool $MANAGED_POOL"
}

api_token_exists() {
    pveum user token list "$API_USER" --output-format json 2>/dev/null \
        | jq -e --arg token "$API_TOKEN_NAME" \
            '.[] | select(.tokenid == $token)' >/dev/null
}

assert_api_token_privsep() {
    local value
    value="$(pveum user token list "$API_USER" --output-format json 2>/dev/null \
        | jq -r --arg token "$API_TOKEN_NAME" \
            '.[] | select(.tokenid == $token) | .privsep // empty')"
    [[ "$value" == "1" ]] || die "PVE API token $API_TOKEN_ID должен иметь privsep=1"
}

token_acl_exists() {
    local path=$1 role=$2
    pveum acl list --output-format json \
        | jq -e \
            --arg path "$path" \
            --arg token "$API_TOKEN_ID" \
            --arg role "$role" \
            '.[] | select(
                .path == $path
                and .type == "token"
                and .ugid == $token
                and .roleid == $role
                and ((.propagate // 1) == 1)
            )' >/dev/null
}

ensure_token_acl() {
    local path=$1 role=$2
    token_acl_exists "$path" "$role" && return

    pveum acl modify "$path" \
        --tokens "$API_TOKEN_ID" \
        --roles "$role" \
        --propagate 1
}

persistent_token_available() {
    ct_exec test -s "$CT_PERSISTENT_SECRET" || return 1
    ct_exec grep -Fxq "PVE_API_TOKEN_ID=$API_TOKEN_ID" "$CT_PERSISTENT_SECRET"
}

stage_api_secret() {
    local secret=$1 node tmp

    node="$(hostname -s)"
    tmp="$(mktemp /run/infra-deployer-pve-api.XXXXXX)"
    chmod 0600 "$tmp"

    cat >"$tmp" <<EOF_TOKEN
PVE_API_URL=https://$node:8006
PVE_API_TOKEN_ID=$API_TOKEN_ID
PVE_API_TOKEN_SECRET=$secret
EOF_TOKEN

    ct_exec install -d -m 0700 "$(dirname "$CT_SECRET_FILE")"
    pct push "$CTID" "$tmp" "$CT_SECRET_FILE" --user 0 --group 0 --perms 0600
    rm -f "$tmp"
}

create_api_token() {
    local json secret

    json="$(pveum user token add "$API_USER" "$API_TOKEN_NAME" \
        --privsep 1 --output-format json)"
    secret="$(jq -r '.value // empty' <<<"$json")"
    [[ -n "$secret" && "$secret" != "null" ]] \
        || die "PVE создал token, но secret не удалось получить"

    stage_api_secret "$secret"
    ok "Создан и передан PVE API token $API_TOKEN_ID"
}

ensure_api_token() {
    if [[ "$MODE" == "recover" ]] && api_token_exists; then
        pveum user token remove "$API_USER" "$API_TOKEN_NAME"
    fi

    if ! api_token_exists; then
        create_api_token
    elif ct_exec test -s "$CT_SECRET_FILE"; then
        ok "PVE API secret уже подготовлен"
    elif persistent_token_available; then
        ok "Используется существующий PVE API token"
    else
        die "Token $API_TOKEN_ID существует, но его secret недоступен в 910. Используйте --recover."
    fi

    assert_api_token_privsep
}

ensure_token_acls() {
    ensure_token_acl "/" "PVEAuditor"
    ensure_token_acl "/pool/$MANAGED_POOL" "PVEVMAdmin"
    ensure_token_acl "/storage/$CT_STORAGE" "PVEDatastoreUser"
    ensure_token_acl "/sdn/zones/localnetwork/$CT_BRIDGE" "PVESDNUser"
    ok "ACL PVE API token подготовлены"
}

install_pve_ca() {
    local src="/etc/pve/pve-root-ca.pem" tmp node host_ip resolved_ip

    [[ -s "$src" ]] || die "Не найден PVE CA: $src"
    tmp="$(mktemp /run/pve-root-ca.XXXXXX.crt)"
    install -m 0644 "$src" "$tmp"

    ct_exec install -d -m 0755 /usr/local/share/ca-certificates
    pct push "$CTID" "$tmp" /usr/local/share/ca-certificates/pve-root-ca.crt \
        --user 0 --group 0 --perms 0644
    rm -f "$tmp"
    ct_exec update-ca-certificates >/dev/null

    node="$(hostname -s)"
    host_ip="$(getent ahostsv4 "$node" | awk 'NR == 1 {print $1}')"
    [[ -n "$host_ip" ]] || die "Не удалось определить IPv4 PVE-узла"

    resolved_ip="$(ct_exec getent ahostsv4 "$node" 2>/dev/null | awk 'NR == 1 {print $1}' || true)"
    if [[ "$resolved_ip" != "$host_ip" ]]; then
        ct_exec sh -c "grep -vE '[[:space:]]$node([[:space:]]|$)' /etc/hosts > /etc/hosts.bootstrap || true; printf '%s %s\n' '$host_ip' '$node' >> /etc/hosts.bootstrap; cat /etc/hosts.bootstrap > /etc/hosts; rm -f /etc/hosts.bootstrap"
    fi

    ct_exec curl -sS --connect-timeout 5 --max-time 15 \
        -o /dev/null "https://$node:8006/api2/json/version" \
        || die "910 не может проверить TLS PVE API"

    ok "PVE CA и имя PVE подготовлены внутри 910"
}

legacy_api_user_exists() {
    pveum user list --output-format json 2>/dev/null \
        | jq -e --arg user "$LEGACY_API_USER"             '.[] | select(.userid == $user)' >/dev/null
}

legacy_api_token_exists() {
    pveum user token list "$LEGACY_API_USER" --output-format json 2>/dev/null \
        | jq -e --arg token "$LEGACY_API_TOKEN_NAME"             '.[] | select(.tokenid == $token)' >/dev/null
}

delete_legacy_acl() {
    local path=$1 role=$2
    pveum acl delete "$path" --users "$LEGACY_API_USER" --roles "$role" >/dev/null 2>&1 || true
    pveum acl delete "$path" --tokens "$LEGACY_API_TOKEN_ID" --roles "$role" >/dev/null 2>&1 || true
}

cleanup_legacy_access() {
    delete_legacy_acl "/" "PVEAuditor"
    delete_legacy_acl "/pool/$MANAGED_POOL" "$LEGACY_ROLE"
    delete_legacy_acl "/vms/$LEGACY_TEMPLATE_VMID" "PVETemplateUser"
    delete_legacy_acl "/storage/$CT_STORAGE" "PVEDatastoreUser"
    delete_legacy_acl "/sdn/zones/localnetwork/$CT_BRIDGE" "PVESDNUser"

    if legacy_api_token_exists; then
        pveum user token remove "$LEGACY_API_USER" "$LEGACY_API_TOKEN_NAME"
    fi
    if legacy_api_user_exists; then
        pveum user delete "$LEGACY_API_USER"
    fi

    pveum role delete "$LEGACY_ROLE" >/dev/null 2>&1 || true
    ok "Устаревшая PVE-идентичность удалена"
}

prepare() {
    install_pve_ca
    ensure_managed_pool
    ensure_api_token
    ensure_token_acls
}

main() {
    require_pve_root

    case "${1:-}" in
        prepare)
            prepare
            ;;
        cleanup)
            cleanup_legacy_access
            ;;
        *)
            die "Использование: $0 prepare|cleanup"
            ;;
    esac
}

main "$@"

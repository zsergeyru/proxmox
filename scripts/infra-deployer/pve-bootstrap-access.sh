#!/usr/bin/env bash
set -Eeuo pipefail

# Этот сценарий хранится в закрытом проекте, но выполняется от root на PVE.
# Он содержит всю PVE-политику, необходимую 910. Гостевой bootstrap готовит
# его внутри 910, а host-bootstrap один раз выполняет его на PVE.

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
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ОК] %s\n' "$*"; }

require_pve_root() {
    [[ $EUID -eq 0 ]] || die "Сценарий должен выполняться от root на PVE"
    command -v pveum >/dev/null 2>&1 || die "Не найден pveum"
    command -v pct >/dev/null 2>&1 || die "Не найден pct"
    command -v perl >/dev/null 2>&1 || die "Не найден штатный Perl PVE"
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
        | perl -MJSON::PP -0777 -e '
            my $token = shift;
            my $rows = decode_json(<STDIN>);
            exit((grep { (($_->{tokenid} // q{}) eq $token) } @$rows) ? 0 : 1);
        ' "$API_TOKEN_NAME"
}

assert_api_token_privsep() {
    local value
    value="$(pveum user token list "$API_USER" --output-format json 2>/dev/null \
        | perl -MJSON::PP -0777 -e '
            my $token = shift;
            my $rows = decode_json(<STDIN>);
            for my $row (@$rows) {
                next unless (($row->{tokenid} // q{}) eq $token);
                print($row->{privsep} // q{});
                last;
            }
        ' "$API_TOKEN_NAME")"
    [[ "$value" == "1" ]] || die "PVE API token $API_TOKEN_ID должен иметь privsep=1"
}

token_acl_exists() {
    local path=$1 role=$2
    pveum acl list --output-format json \
        | perl -MJSON::PP -0777 -e '
            my ($path, $token, $role) = @ARGV;
            my $rows = decode_json(<STDIN>);
            for my $row (@$rows) {
                next unless (($row->{path} // q{}) eq $path);
                next unless (($row->{type} // q{}) eq q{token});
                next unless (($row->{ugid} // q{}) eq $token);
                next unless (($row->{roleid} // q{}) eq $role);
                next unless (($row->{propagate} // 1) == 1);
                exit 0;
            }
            exit 1;
        ' "$path" "$API_TOKEN_ID" "$role"
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
    secret="$(perl -MJSON::PP -0777 -e '
        my $row = decode_json(<STDIN>);
        print($row->{value} // q{});
    ' <<<"$json")"
    [[ -n "$secret" ]] \
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

prepare() {
    install_pve_ca
    ensure_managed_pool
    ensure_api_token
    ensure_token_acls
}

main() {
    require_pve_root
    prepare
}

main "$@"

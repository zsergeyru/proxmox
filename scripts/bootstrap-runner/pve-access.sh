#!/usr/bin/env bash
set -Eeuo pipefail

MODE="${BOOTSTRAP_RUNNER_MODE:-apply}"
CTID="${BOOTSTRAP_RUNNER_CTID:-990}"
API_USER="root@pam"
API_TOKEN_NAME="bootstrap-runner"
API_TOKEN_ID="${API_USER}!${API_TOKEN_NAME}"
CT_STORAGE="local-lvm"
CT_BRIDGE="vmbr0"
CT_SECRET_DIR="/etc/bootstrap-runner/secrets"
CT_SECRET_FILE="${CT_SECRET_DIR}/pve-api.env"
CT_CA_FILE="/etc/bootstrap-runner/ca/ca-bundle.crt"

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok() { printf '[ОК] %s\n' "$*"; }

require_host() {
    [[ $EUID -eq 0 ]] || die "сценарий должен выполняться от root на PVE"
    command -v pveum >/dev/null 2>&1 || die "не найден pveum"
    command -v perl >/dev/null 2>&1 || die "не найден Perl"
    if [[ "$MODE" == "apply" ]]; then
        command -v pct >/dev/null 2>&1 || die "не найден pct"
        pct status "$CTID" >/dev/null 2>&1 || die "LXC $CTID отсутствует"
    fi
}

token_exists() {
    pveum user token list "$API_USER" --output-format json 2>/dev/null         | perl -MJSON::PP -0777 -e '
            my $token = shift;
            my $rows = decode_json(<STDIN>);
            exit((grep { (($_->{tokenid} // q{}) eq $token) } @$rows) ? 0 : 1);
        ' "$API_TOKEN_NAME"
}

remove_token_acls() {
    local entry path role
    while IFS= read -r entry; do
        [[ -n "$entry" ]] || continue
        path=${entry%%|*}
        role=${entry#*|}
        pveum acl delete "$path" --tokens "$API_TOKEN_ID" --roles "$role" || true
    done < <(
        pveum acl list --output-format json             | perl -MJSON::PP -0777 -e '
                my $token = shift;
                my $rows = decode_json(<STDIN>);
                for my $row (@$rows) {
                    next unless (($row->{type} // q{}) eq q{token});
                    next unless (($row->{ugid} // q{}) eq $token);
                    print(($row->{path} // q{}), q{|}, ($row->{roleid} // q{}), qq{\n});
                }
            ' "$API_TOKEN_ID"
    )
}

remove_access() {
    remove_token_acls
    if token_exists; then
        pveum user token remove "$API_USER" "$API_TOKEN_NAME"
    fi
    ok "Временный PVE API-доступ 990 удалён"
}

ensure_acl() {
    local path=$1 role=$2
    pveum acl modify "$path"         --tokens "$API_TOKEN_ID"         --roles "$role"         --propagate 1
}

stage_access() (
    # Подоболочка нужна намеренно: EXIT trap видит локальные пути до самого
    # завершения блока и удаляет временные файлы как при успехе, так и при ошибке.
    local secret=$1 node host_ip tmp ca_tmp

    node="$(hostname -s)"
    host_ip="$(getent ahostsv4 "$node" | awk 'NR == 1 {print $1}')"
    [[ -n "$host_ip" ]] || die "не удалось определить IPv4 PVE-узла"

    pct exec "$CTID" -- install -d -m 0700 "$CT_SECRET_DIR"
    pct exec "$CTID" -- install -d -m 0755 "$(dirname "$CT_CA_FILE")"

    tmp="$(mktemp /run/bootstrap-runner-pve-api.XXXXXX)"
    ca_tmp="$(mktemp /run/bootstrap-runner-pve-ca.XXXXXX)"
    trap 'rm -f -- "$tmp" "$ca_tmp"' EXIT

    chmod 0600 "$tmp"
    install -m 0644 /etc/pve/pve-root-ca.pem "$ca_tmp"

    cat >"$tmp" <<EOF
PVE_API_URL=https://$node:8006
PVE_API_TOKEN_ID=$API_TOKEN_ID
PVE_API_TOKEN_SECRET=$secret
TF_VAR_pve_endpoint=https://$node:8006
TF_VAR_pve_api_token=$API_TOKEN_ID=$secret
EOF

    pct push "$CTID" "$tmp" "$CT_SECRET_FILE" --user 0 --group 0 --perms 0600
    pct push "$CTID" "$ca_tmp" "$CT_CA_FILE" --user 0 --group 0 --perms 0644

    pct exec "$CTID" -- sh -c '
        node=$1
        ip=$2
        grep -vE "[[:space:]]${node}([[:space:]]|$)" /etc/hosts > /etc/hosts.bootstrap || true
        printf "%s %s\n" "$ip" "$node" >> /etc/hosts.bootstrap
        cat /etc/hosts.bootstrap > /etc/hosts
        rm -f /etc/hosts.bootstrap
    ' sh "$node" "$host_ip"
)

create_access() {
    local json secret

    remove_token_acls
    if token_exists; then
        pveum user token remove "$API_USER" "$API_TOKEN_NAME"
    fi

    json="$(pveum user token add "$API_USER" "$API_TOKEN_NAME" --privsep 1 --output-format json)"
    secret="$(perl -MJSON::PP -0777 -e '
        my $row = decode_json(<STDIN>);
        print($row->{value} // q{});
    ' <<<"$json")"
    [[ -n "$secret" ]] || {
        pveum user token remove "$API_USER" "$API_TOKEN_NAME" || true
        die "PVE создал token без доступного secret"
    }

    ensure_acl "/" "PVEAuditor"
    ensure_acl "/vms" "PVEVMAdmin"
    ensure_acl "/storage/$CT_STORAGE" "PVEDatastoreUser"
    ensure_acl "/storage/local" "PVEDatastoreUser"
    ensure_acl "/sdn/zones/localnetwork/$CT_BRIDGE" "PVESDNUser"

    if ! stage_access "$secret"; then
        remove_access || true
        die "не удалось передать временный PVE API-доступ в 990"
    fi

    ok "Временный PVE API-доступ 990 подготовлен"
}

main() {
    require_host
    case "$MODE" in
        apply) create_access ;;
        remove) remove_access ;;
        *) die "неизвестный режим: $MODE" ;;
    esac
}

main "$@"

#!/usr/bin/env bash
set -Eeuo pipefail

CTID="${BOOTSTRAP_RUNNER_CTID:-990}"
MODE="${BOOTSTRAP_RUNNER_MODE:-apply}"
API_USER="root@pam"
API_TOKEN_NAME="bootstrap-runner"
API_TOKEN_ID="${API_USER}!${API_TOKEN_NAME}"

CT_CA_DIR="/etc/bootstrap-runner/ca"
CT_CA_FILE="${CT_CA_DIR}/ca-bundle.crt"
CT_SECRET_DIR="/etc/bootstrap-runner/secrets"
CT_SECRET_FILE="${CT_SECRET_DIR}/pve-api.env"

CT_STORAGE="local-lvm"
CT_BRIDGE="vmbr0"

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok() { printf '[ОК] %s\n' "$*"; }

require_pve_root() {
    [[ $EUID -eq 0 ]] || die "Сценарий должен выполняться от root на PVE"
    command -v pveum >/dev/null 2>&1 || die "Не найден pveum"
    command -v pct >/dev/null 2>&1 || die "Не найден pct"
    command -v perl >/dev/null 2>&1 || die "Не найден штатный Perl PVE"
}

ct_exists() {
    pct config "$CTID" >/dev/null 2>&1
}

api_token_exists() {
    pveum user token list "$API_USER" --output-format json 2>/dev/null \
        | perl -MJSON::PP -0777 -e '
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
        [[ -n "$path" && -n "$role" ]] || continue
        pveum acl delete "$path" \
            --tokens "$API_TOKEN_ID" \
            --roles "$role"
    done < <(
        pveum acl list --output-format json \
            | perl -MJSON::PP -0777 -e '
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

remove_token() {
    remove_token_acls
    if api_token_exists; then
        pveum user token remove "$API_USER" "$API_TOKEN_NAME"
    fi
}

stage_ca() {
    local source="/etc/pve/pve-root-ca.pem"
    [[ -s "$source" ]] || die "Не найден PVE CA: $source"
    pct exec "$CTID" -- install -d -m 0755 "$CT_CA_DIR"
    pct push "$CTID" "$source" "$CT_CA_FILE" \
        --user 0 --group 0 --perms 0644
}

stage_secret() {
    local secret=$1 node tmp
    node="$(hostname -s)"
    tmp="$(mktemp /run/bootstrap-runner-pve-api.XXXXXX)"
    trap 'rm -f -- "$tmp"' RETURN
    chmod 0600 "$tmp"

    cat >"$tmp" <<EOF
PVE_API_URL=https://$node:8006
PVE_API_TOKEN_ID=$API_TOKEN_ID
PVE_API_TOKEN_SECRET=$secret
EOF

    pct exec "$CTID" -- install -d -m 0700 "$CT_SECRET_DIR"
    pct push "$CTID" "$tmp" "$CT_SECRET_FILE" \
        --user 0 --group 0 --perms 0600
    rm -f -- "$tmp"
    trap - RETURN
}

create_token() {
    local json secret
    json="$(pveum user token add "$API_USER" "$API_TOKEN_NAME" \
        --privsep 1 --output-format json)"
    secret="$(perl -MJSON::PP -0777 -e '
        my $row = decode_json(<STDIN>);
        print($row->{value} // q{});
    ' <<<"$json")"
    [[ -n "$secret" ]] || {
        remove_token
        die "PVE создал token без доступного secret"
    }

    if ! stage_secret "$secret"; then
        remove_token
        die "Не удалось передать временный PVE API secret в 990"
    fi
}

set_acls() {
    pveum acl modify "/" \
        --tokens "$API_TOKEN_ID" \
        --roles PVEAuditor \
        --propagate 1
    pveum acl modify "/vms" \
        --tokens "$API_TOKEN_ID" \
        --roles PVEVMAdmin \
        --propagate 1
    pveum acl modify "/storage/$CT_STORAGE" \
        --tokens "$API_TOKEN_ID" \
        --roles PVEDatastoreUser \
        --propagate 1
    pveum acl modify "/sdn/zones/localnetwork/$CT_BRIDGE" \
        --tokens "$API_TOKEN_ID" \
        --roles PVESDNUser \
        --propagate 1
}

apply() {
    ct_exists || die "LXC $CTID отсутствует"
    remove_token
    create_token
    set_acls
    stage_ca
    ok "Временный PVE API-доступ bootstrap-runner подготовлен"
}

cleanup() {
    remove_token
    if ct_exists; then
        pct exec "$CTID" -- rm -f "$CT_SECRET_FILE" "$CT_CA_FILE" || true
    fi
    ok "Временный PVE API-доступ bootstrap-runner удалён"
}

main() {
    require_pve_root
    case "$MODE" in
        apply) apply ;;
        cleanup) cleanup ;;
        *) die "Неизвестный режим: $MODE" ;;
    esac
}

main "$@"

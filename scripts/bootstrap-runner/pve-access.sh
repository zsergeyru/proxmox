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

HOST_DIR="/var/lib/proxmox-bootstrap"
HOST_STATE_DIR="${HOST_DIR}/state"
HOST_STATE_FILE="${HOST_STATE_DIR}/910.tfstate"
HOST_SSH_DIR="${HOST_DIR}/ssh"
HOST_SSH_KEY="${HOST_SSH_DIR}/910-bootstrap-ed25519"
HOST_SSH_PUB="${HOST_SSH_KEY}.pub"

CT_STATE_DIR="/var/lib/bootstrap-runner/opentofu/state"
CT_STATE_FILE="${CT_STATE_DIR}/proxmox.tfstate"
CT_SSH_DIR="/var/lib/bootstrap-runner/ssh"
CT_SSH_KEY="${CT_SSH_DIR}/guest_ed25519"
CT_SSH_PUB="${CT_SSH_KEY}.pub"

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok() { printf '[ОК] %s\n' "$*"; }

require_pve_root() {
    [[ $EUID -eq 0 ]] || die "Сценарий должен выполняться от root на PVE"
    command -v pveum >/dev/null 2>&1 || die "Не найден pveum"
    command -v pct >/dev/null 2>&1 || die "Не найден pct"
    command -v perl >/dev/null 2>&1 || die "Не найден штатный Perl PVE"
    command -v ssh-keygen >/dev/null 2>&1 || die "Не найден ssh-keygen"
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

ensure_bootstrap_key() {
    install -d -o root -g root -m 0700 "$HOST_SSH_DIR"
    if [[ ! -s "$HOST_SSH_KEY" ]]; then
        rm -f "$HOST_SSH_KEY" "$HOST_SSH_PUB"
        ssh-keygen -q -t ed25519 -N '' \
            -C "bootstrap-runner to 910" \
            -f "$HOST_SSH_KEY"
    fi
    local public_key
    public_key="$(ssh-keygen -y -f "$HOST_SSH_KEY" 2>/dev/null)" \
        || die "Повреждён bootstrap SSH key 910"
    [[ -n "$public_key" ]] || die "Пустой public key bootstrap SSH 910"
    printf '%s %s\n' "$public_key" "bootstrap-runner to 910" >"$HOST_SSH_PUB"
    chmod 0600 "$HOST_SSH_KEY"
    chmod 0644 "$HOST_SSH_PUB"
}

stage_bootstrap_key() {
    ensure_bootstrap_key
    pct exec "$CTID" -- install -d -m 0700 "$CT_SSH_DIR"
    pct push "$CTID" "$HOST_SSH_KEY" "$CT_SSH_KEY" \
        --user 0 --group 0 --perms 0600
    pct push "$CTID" "$HOST_SSH_PUB" "$CT_SSH_PUB" \
        --user 0 --group 0 --perms 0644
}

stage_existing_state() {
    install -d -o root -g root -m 0700 "$HOST_STATE_DIR"
    pct exec "$CTID" -- install -d -m 0700 "$CT_STATE_DIR"

    if pct exec "$CTID" -- test -s "$CT_STATE_FILE"; then
        ok "Используется существующий рабочий bootstrap-state внутри 990"
        return
    fi
    if [[ -s "$HOST_STATE_FILE" ]]; then
        pct push "$CTID" "$HOST_STATE_FILE" "$CT_STATE_FILE" \
            --user 0 --group 0 --perms 0600
        ok "Bootstrap-state 910 передан в новый 990"
    fi
}

save_state() {
    local tmp
    ct_exists || die "LXC $CTID отсутствует"
    pct exec "$CTID" -- test -s "$CT_STATE_FILE" \
        || die "В 990 отсутствует bootstrap-state 910"

    install -d -o root -g root -m 0700 "$HOST_STATE_DIR"
    tmp="$(mktemp "${HOST_STATE_DIR}/910.tfstate.XXXXXX")"
    trap 'rm -f -- "$tmp"' RETURN
    pct pull "$CTID" "$CT_STATE_FILE" "$tmp"
    chmod 0600 "$tmp"
    mv -f "$tmp" "$HOST_STATE_FILE"
    trap - RETURN
    ok "Bootstrap-state 910 сохранён на PVE"
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
    stage_bootstrap_key
    stage_existing_state
    ok "Временное окружение bootstrap-runner подготовлено"
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
        save-state) save_state ;;
        cleanup) cleanup ;;
        *) die "Неизвестный режим: $MODE" ;;
    esac
}

main "$@"

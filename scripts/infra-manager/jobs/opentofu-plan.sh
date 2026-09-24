#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
OPENTOFU_DIR="$REPO_ROOT/automation/opentofu"
STATE_DIR="/var/lib/infra-manager/opentofu"
GUEST_STATE_FILE="$STATE_DIR/guests.json"
CA_BUNDLE="/etc/infra-manager/ca/ca-bundle.crt"

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ОК] %s\n' "$*"; }

command -v python3 >/dev/null 2>&1 || die "Не найден python3"
command -v tofu >/dev/null 2>&1 || die "Не найден OpenTofu"

[[ -n "${TF_VAR_pve_endpoint:-}" ]]     || die "Не задан TF_VAR_pve_endpoint"
[[ -n "${TF_VAR_pve_api_token:-}" ]]     || die "Не задан TF_VAR_pve_api_token"
[[ -d "$OPENTOFU_DIR" ]]     || die "Не найден каталог OpenTofu: $OPENTOFU_DIR"
[[ -s "$CA_BUNDLE" ]] || die "Не найден CA bundle: $CA_BUNDLE"

install -d -m 0750 "$STATE_DIR"
umask 077

# OpenTofu provider использует системный TLS стек Go.
# Передаём ему подготовленный bundle с PVE CA, не отключая проверку сертификата.
export SSL_CERT_FILE="$CA_BUNDLE"

python3 "$REPO_ROOT/scripts/guests/render-opentofu-input.py"     --output "$GUEST_STATE_FILE"

tofu -chdir="$OPENTOFU_DIR" init     -input=false     -no-color     -lockfile=readonly

set +e
tofu -chdir="$OPENTOFU_DIR" plan     -input=false     -no-color     -lock-timeout=30s     -detailed-exitcode
rc=$?
set -e

case "$rc" in
    0)
        ok "OpenTofu: изменений нет"
        ;;
    2)
        ok "OpenTofu: план содержит изменения"
        ;;
    *)
        die "OpenTofu plan завершился с кодом $rc"
        ;;
esac

#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
OPENTOFU_DIR="$REPO_ROOT/opentofu"
STATE_DIR="/var/lib/infra-deployer/opentofu"
GUEST_STATE_FILE="$STATE_DIR/guests.json"

die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
ok()  { printf '[ОК] %s\n' "$*"; }

command -v python3 >/dev/null 2>&1 || die "Не найден python3"
command -v tofu >/dev/null 2>&1 || die "Не найден OpenTofu"

[[ -n "${TF_VAR_pve_endpoint:-}" ]]     || die "Не задан TF_VAR_pve_endpoint"
[[ -n "${TF_VAR_pve_api_token:-}" ]]     || die "Не задан TF_VAR_pve_api_token"
[[ -d "$OPENTOFU_DIR" ]]     || die "Не найден каталог OpenTofu: $OPENTOFU_DIR"

install -d -m 0750 "$STATE_DIR"
umask 077

python3 "$REPO_ROOT/scripts/infra-deployer/render-opentofu-input.py"     --output "$GUEST_STATE_FILE"

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

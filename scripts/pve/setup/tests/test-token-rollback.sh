#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"
# shellcheck source=/dev/null
source "${ROOT}/scripts/pve/setup/lib/00-common.sh"
# shellcheck source=/dev/null
source "${ROOT}/scripts/pve/setup/lib/50-access.sh"

calls="$(mktemp)"
trap 'rm -f "$calls"' EXIT

pveum() {
    printf '%s\n' "$*" >>"$calls"
    return 0
}

warn() { :; }
die() { exit 99; }

set +e
(
    rollback_new_token deployer@pve host-deploy 'deployer@pve!host-deploy' 'forced test rollback'
)
rc=$?
set -e

[[ "$rc" == "99" ]] || {
    printf 'rollback_new_token returned unexpected rc=%s\n' "$rc" >&2
    exit 1
}

grep -Fxq 'user token delete deployer@pve host-deploy' "$calls" || {
    printf 'rollback_new_token did not use current pveum user token delete syntax\n' >&2
    cat "$calls" >&2
    exit 1
}

: >"$calls"
PENDING_TOKEN_USER='ai-agent@pve'
PENDING_TOKEN_NAME='infra'
PENDING_TOKEN_FULL='ai-agent@pve!infra'
cleanup_pending_token

grep -Fxq 'user token delete ai-agent@pve infra' "$calls" || {
    printf 'cleanup_pending_token did not delete the pending token\n' >&2
    cat "$calls" >&2
    exit 1
}
[[ -z "$PENDING_TOKEN_USER" && -z "$PENDING_TOKEN_NAME" && -z "$PENDING_TOKEN_FULL" ]] || {
    printf 'cleanup_pending_token did not clear pending token state\n' >&2
    exit 1
}

printf 'Token rollback tests passed.\n'

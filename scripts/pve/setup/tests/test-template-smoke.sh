#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"

TEMPLATE_VMID=9000
TEMPLATE_VERSION=7
TEMPLATE_WAIT_SECONDS=1200
TEMPLATE_WAIT_PROGRESS_SECONDS=15
SMOKE_VMID=9099
SMOKE_NAME="smoke-debian13-9099"
SMOKE_DISK_SIZE="20G"
SMOKE_MIN_ROOT_BYTES=$((18 * 1024 * 1024 * 1024))
SMOKE_TEST_TEMPLATE=0
TEMPLATE_BUILT_THIS_RUN=0
SMOKE_ACTIVE=0
STATE_DIR="$(mktemp -d)"
TEMPLATE_SMOKE_STATE_FILE="${STATE_DIR}/template-smoke.json"
trap 'rm -rf "$STATE_DIR"' EXIT

C_BOLD='' C_YELLOW='' C_RESET=''
PVE_GUEST_KEY=/tmp/not-used
PVE_GUEST_PUB=/tmp/not-used
TEMPLATE_DISK_STORAGE=local-lvm

state_revision() { printf '%s\n' '0123456789012345678901234567890123456789'; }
info() { :; }
ok() { :; }
log() { :; }
wait_msg() { :; }
require_cmd() { :; }
die() { printf 'die: %s\n' "$*" >&2; return 1; }
cleanup_smoke_known_hosts() { :; }
template_size_to_kib() { :; }
validate_template_contract() { :; }
template_guest_exec() { :; }

# shellcheck source=/dev/null
source "${ROOT}/scripts/pve/setup/lib/63-template-smoke.sh"

QM_CASE=free
qm() {
    if [[ "$1" != 'config' ]]; then
        return 2
    fi
    case "$QM_CASE" in
        free) return 1 ;;
        owned)
            printf '%s\n' "name: ${SMOKE_NAME}"
            printf 'description: PVE Configuration Full Clone smoke test; %s\n' "$(smoke_marker)"
            ;;
        foreign)
            printf '%s\n' 'name: ordinary-vm'
            printf '%s\n' 'description: unrelated workload'
            ;;
        *) return 2 ;;
    esac
}

PCT_CASE=free
pct() {
    if [[ "$1" != 'config' ]]; then
        return 2
    fi
    [[ "$PCT_CASE" == 'lxc' ]]
}

[[ "$(smoke_vmid_state)" == 'free' ]]

QM_CASE=owned
[[ "$(smoke_vmid_state)" == 'owned-smoke' ]]

QM_CASE=foreign
[[ "$(smoke_vmid_state)" == 'foreign-vm' ]]

QM_CASE=free
PCT_CASE=lxc
[[ "$(smoke_vmid_state)" == 'foreign-lxc' ]]
PCT_CASE=free

rm -f "$TEMPLATE_SMOKE_STATE_FILE"
SMOKE_TEST_TEMPLATE=0
TEMPLATE_BUILT_THIS_RUN=0
if smoke_test_required; then
    printf 'Smoke test unexpectedly required without build/flag/pending state\n' >&2
    exit 1
fi

SMOKE_TEST_TEMPLATE=1
smoke_test_required
SMOKE_TEST_TEMPLATE=0

TEMPLATE_BUILT_THIS_RUN=1
smoke_test_required
TEMPLATE_BUILT_THIS_RUN=0

cat >"$TEMPLATE_SMOKE_STATE_FILE" <<EOF_PENDING
{
  "status": "pending",
  "template_vmid": ${TEMPLATE_VMID},
  "template_version": ${TEMPLATE_VERSION}
}
EOF_PENDING
[[ "$(template_smoke_state_status)" == 'pending' ]]
smoke_test_required

cat >"$TEMPLATE_SMOKE_STATE_FILE" <<EOF_PASSED
{
  "status": "passed",
  "template_vmid": ${TEMPLATE_VMID},
  "template_version": ${TEMPLATE_VERSION}
}
EOF_PASSED
[[ "$(template_smoke_state_status)" == 'passed' ]]
if smoke_test_required; then
    printf 'Smoke test unexpectedly required for passed state without explicit flag\n' >&2
    exit 1
fi

# Защитные invariants реализации: только Full Clone и удаление только в success path.
grep -q -- '--full 1' "${ROOT}/scripts/pve/setup/lib/63-template-smoke.sh"
grep -q 'qm destroy' "${ROOT}/scripts/pve/setup/lib/63-template-smoke.sh"
grep -q 'SMOKE_ACTIVE=1' "${ROOT}/scripts/pve/setup/lib/63-template-smoke.sh"
grep -q 'SMOKE_ACTIVE=0' "${ROOT}/scripts/pve/setup/lib/63-template-smoke.sh"

printf 'Template smoke safety tests passed.\n'

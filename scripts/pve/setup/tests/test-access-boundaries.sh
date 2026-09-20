#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"

# shellcheck source=/dev/null
source "$ROOT/scripts/pve/setup/lib/00-common.sh"
# shellcheck source=/dev/null
source "$ROOT/scripts/pve/setup/lib/50-access.sh"

WARN_COUNT=0
AI_PERMISSION_BOUNDARY_WARNINGS=0

allowed_json='{
  "/vms/9000": {"VM.Audit": 1, "VM.Clone": 1},
  "/vms": {"VM.Audit": 1}
}'

warn_unexpected_permission_set "ai-agent@pve!infra" "$allowed_json" "/vms/9000" "$ROLE_CLONE_PRIVS"
[[ "$WARN_COUNT" -eq 0 && "$AI_PERMISSION_BOUNDARY_WARNINGS" -eq 0 ]] || {
    printf 'Allowed template permissions produced a warning\n' >&2
    exit 1
}

extra_json='{
  "/vms/9000": {"VM.Audit": 1, "VM.Clone": 1, "VM.PowerMgmt": 1},
  "/vms/302": {"VM.Audit": 1, "VM.PowerMgmt": 1},
  "/access": {"Permissions.Modify": 1}
}'

warn_unexpected_permission_set "ai-agent@pve!infra" "$extra_json" "/vms/9000" "$ROLE_CLONE_PRIVS"
[[ "$WARN_COUNT" -eq 1 && "$AI_PERMISSION_BOUNDARY_WARNINGS" -eq 1 ]] || {
    printf 'Extra template permission was not reported as boundary warning\n' >&2
    exit 1
}

warn_unexpected_vm_scope_permissions "ai-agent@pve!infra" "$extra_json"
[[ "$WARN_COUNT" -eq 2 ]] || {
    printf 'Direct mutation permission on an unmanaged VM was not reported\n' >&2
    exit 1
}

warn_forbidden_permission_anywhere "ai-agent@pve!infra" "$extra_json" "$AI_FORBIDDEN_ADMIN_PRIVS"
[[ "$WARN_COUNT" -eq 3 ]] || {
    printf 'Administrative permission outside root path was not reported\n' >&2
    exit 1
}

AI_PERMISSION_BOUNDARY_WARNINGS=0
warn_forbidden_permission_anywhere "deployer@pve!host-deploy" "$extra_json" "$AI_FORBIDDEN_ADMIN_PRIVS" 0
[[ "$WARN_COUNT" -eq 4 && "$AI_PERMISSION_BOUNDARY_WARNINGS" -eq 0 ]] || {
    printf 'Deployer administrative permission must warn without setting the AI boundary flag\n' >&2
    exit 1
}

pveum() {
    if [[ "$1 $2 $3" == "acl list --output-format" ]]; then
        cat <<'JSON'
[
  {"path":"/vms","type":"user","ugid":"deployer@pve","roleid":"AIManagedGuest","propagate":1},
  {"path":"/","type":"user","ugid":"deployer@pve","roleid":"Administrator","propagate":1},
  {"path":"/pool/managed","type":"user","ugid":"ai-agent@pve","roleid":"AIManagedGuest","propagate":1},
  {"path":"/pool/managed","type":"user","ugid":"ai-agent@pve","roleid":"AIManagedPool","propagate":1},
  {"path":"/vms/9000","type":"token","ugid":"ai-agent@pve!infra","roleid":"AICloneSource","propagate":1},
  {"path":"/vms","type":"token","ugid":"ai-agent@pve!infra","roleid":"AIManagedGuest","propagate":1}
]
JSON
        return 0
    fi
    return 1
}

warn_unexpected_host_acl_entries "deployer@pve" "deployer@pve!host-deploy"
[[ "$WARN_COUNT" -eq 5 ]] || {
    printf 'Unexpected deployer ACL was not reported exactly once\n' >&2
    exit 1
}

warn_unexpected_ai_acl_entries "ai-agent@pve" "ai-agent@pve!infra"
[[ "$WARN_COUNT" -eq 6 ]] || {
    printf 'Unexpected AI ACL was not reported exactly once\n' >&2
    exit 1
}

grep -Fq 'warn_unexpected_host_acl_entries "$HOST_PVE_USER" "$HOST_PVE_TOKEN"' "$ROOT/scripts/pve/setup/lib/50-access.sh" \
    || { printf 'Deployer ACL boundary check is not wired into configuration\n' >&2; exit 1; }
grep -Fq 'warn_unexpected_ai_acl_entries "$AI_PVE_USER" "$AI_PVE_TOKEN"' "$ROOT/scripts/pve/setup/lib/50-access.sh" \
    || { printf 'AI ACL boundary check is not wired into configuration\n' >&2; exit 1; }

printf 'AI permission boundary tests passed.\n'

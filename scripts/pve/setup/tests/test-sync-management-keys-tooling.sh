#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"
CONFIGURE="$ROOT/scripts/pve/setup/configure-pve.sh"
TOOLING="$ROOT/scripts/pve/setup/lib/71-sync-management-keys-tooling.sh"
SOURCE="$ROOT/scripts/pve/sync-management-keys.py"

fail() {
    printf 'sync-management-keys tooling contract failed: %s\n' "$*" >&2
    exit 1
}

grep -Fq '    71-sync-management-keys-tooling.sh' "$CONFIGURE" \
    || fail "PVE Configuration must source sync-management-keys tooling module"
grep -Fq '    install_sync_management_keys_tooling' "$CONFIGURE" \
    || fail "PVE Configuration must install sync-management-keys command"
grep -Fq 'chmod 0700 /usr/local/sbin/sync-management-keys' "$TOOLING" \
    || fail "manual sync command must remain root-only"
grep -Fq 'runuser -u "\$DEPLOY_USER" -- env' "$TOOLING" \
    || fail "sync runtime must execute as pvedeploy"
grep -Fq 'SYNC_MANAGEMENT_KEYS_SOURCE_REVISION="\$revision"' "$TOOLING" \
    || fail "wrapper must pin source revision"
grep -Fq 'flock -n 9' "$TOOLING" \
    || fail "manual sync command must use orchestration lock"
grep -Fq 'management-ssh' "$SOURCE" \
    || fail "runtime must discover management-ssh participants"
grep -Fq 'BEGIN PROXMOX-MANAGEMENT-KEYS' "$SOURCE" \
    || fail "managed authorized_keys block marker is missing"
grep -Fq 'StrictHostKeyChecking=yes' "$SOURCE" \
    || fail "strict SSH host-key verification is missing"
grep -Fq 'guest не запускается ради sync' "$SOURCE" \
    || fail "offline guests must not be started for synchronization"

printf 'sync-management-keys tooling contract tests passed.\n'

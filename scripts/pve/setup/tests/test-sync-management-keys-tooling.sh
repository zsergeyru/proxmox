#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"
CONFIGURE="$ROOT/scripts/pve/setup/configure-pve.sh"
TOOLING="$ROOT/scripts/pve/setup/lib/71-sync-management-keys-tooling.sh"
RUNTIME_SETUP="$ROOT/scripts/pve/setup/lib/40-runtime.sh"
COMMON="$ROOT/scripts/pve/setup/lib/00-common.sh"
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
grep -Fq 'OWNERSHIP_TAG = "proxmox-deployer"' "$SOURCE" \
    || fail "runtime must require project ownership before guest mutation"
grep -Fq 'manifest = find_manifest(vmid)' "$SOURCE" \
    || fail "runtime must derive participants from project manifests"
grep -Fq 'if not source.get("profile"):' "$SOURCE" \
    || fail "runtime must use profile as the administrative SSH participation contract"
if grep -Fq 'management-ssh' "$SOURCE"; then
    fail "legacy management-ssh participation tag must not remain in runtime"
fi
grep -Fq 'DEPLOYER_REGISTRY_KEY = REGISTRY_DIR / "pve_deployer_ed25519.pub"' "$SOURCE" \
    || fail "sync runtime must use canonical PVE deployer registry filename"
grep -Fq 'PVE_CA_FILE="${CONFIG_DIR}/pve-root-ca.pem"' "$COMMON" \
    || fail "runtime PVE CA destination must be defined outside /etc/pve"
grep -Fq 'install -o root -g "$DEPLOY_USER" -m 0640 "$PVE_CA_SOURCE" "$PVE_CA_FILE"' "$RUNTIME_SETUP" \
    || fail "PVE Configuration must provision a read-only runtime PVE CA copy"
grep -Fq 'PVE_CA = CONFIG_DIR / "pve-root-ca.pem"' "$SOURCE" \
    || fail "sync runtime must use the provisioned PVE CA copy"
grep -Fq 'safe.directory={ROOT}' "$SOURCE" \
    || fail "runtime Git revision check must trust only the canonical root-owned checkout locally"
grep -Fq 'BEGIN PROXMOX-MANAGEMENT-KEYS' "$SOURCE" \
    || fail "managed authorized_keys block marker is missing"
grep -Fq 'StrictHostKeyChecking=yes' "$SOURCE" \
    || fail "strict SSH host-key verification is missing"
grep -Fq 'guest не запускается ради sync' "$SOURCE" \
    || fail "offline guests must not be started for synchronization"

printf 'sync-management-keys tooling contract tests passed.\n'

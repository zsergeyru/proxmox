#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"
MODULE="$ROOT/scripts/pve/setup/lib/45-management-keys.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() {
    printf 'Management key registry test failed: %s\n' "$*" >&2
    exit 1
}

log() { :; }
ok() { :; }
die() {
    printf 'expected failure: %s\n' "$*" >&2
    exit 1
}

RUNTIME_DIR="$TMP/runtime"
DEPLOY_USER="root"
PVE_DEPLOYER_KEY="$TMP/pve_deployer_ed25519"
PVE_DEPLOYER_PUB="${PVE_DEPLOYER_KEY}.pub"

ssh-keygen -q -t ed25519 -N '' -C 'test-deployer' -f "$PVE_DEPLOYER_KEY"

# shellcheck source=/dev/null
source "$MODULE"

ensure_management_public_key_registry

[[ -d "$PUBLIC_KEYS_DIR" ]] || fail "public-key registry was not created"
[[ "$(stat -c '%a' "$PUBLIC_KEYS_DIR")" == "750" ]] || fail "registry mode must be 0750"
[[ -f "$PVE_DEPLOYER_REGISTRY_KEY" ]] || fail "pve_deployer_ed25519.pub was not created"
[[ "$(stat -c '%a' "$PVE_DEPLOYER_REGISTRY_KEY")" == "644" ]] || fail "pve_deployer_ed25519.pub mode must be 0644"
[[ -f "$MANAGEMENT_KEYS_AGGREGATE" ]] || fail "management-authorized-keys was not created"

source_fp="$(ssh-keygen -lf "$PVE_DEPLOYER_PUB" -E sha256 | awk '{print $2}')"
registry_fp="$(ssh-keygen -lf "$PVE_DEPLOYER_REGISTRY_KEY" -E sha256 | awk '{print $2}')"
[[ "$source_fp" == "$registry_fp" ]] || fail "pve_deployer_ed25519.pub fingerprint differs from deployer identity"
[[ "$(awk 'NF {count++} END {print count+0}' "$MANAGEMENT_KEYS_AGGREGATE")" == "1" ]] \
    || fail "initial aggregate must contain only pve_deployer_ed25519.pub"

ssh-keygen -q -t ed25519 -N '' -C 'guest-301' -f "$TMP/guest301"
cp "$TMP/guest301.pub" "$PUBLIC_KEYS_DIR/301.pub"
ensure_management_public_key_registry

[[ "$(awk 'NF {count++} END {print count+0}' "$MANAGEMENT_KEYS_AGGREGATE")" == "2" ]] \
    || fail "aggregate must contain pve_deployer_ed25519.pub and 301.pub"
first_line="$(sed -n '1p' "$MANAGEMENT_KEYS_AGGREGATE")"
second_line="$(sed -n '2p' "$MANAGEMENT_KEYS_AGGREGATE")"
deployer_line="$(awk 'NF {print; exit}' "$PVE_DEPLOYER_REGISTRY_KEY")"
guest_line="$(awk 'NF {print; exit}' "$PUBLIC_KEYS_DIR/301.pub")"
[[ "$first_line" == "$deployer_line" ]] || fail "pve_deployer_ed25519.pub must be first in deterministic aggregate"
[[ "$second_line" == "$guest_line" ]] || fail "301.pub must follow pve_deployer_ed25519.pub in aggregate"

aggregate_before="$(sha256sum "$MANAGEMENT_KEYS_AGGREGATE" | awk '{print $1}')"
ensure_management_public_key_registry
aggregate_after="$(sha256sum "$MANAGEMENT_KEYS_AGGREGATE" | awk '{print $1}')"
[[ "$aggregate_before" == "$aggregate_after" ]] || fail "idempotent rebuild changed aggregate contents"

cp "$PUBLIC_KEYS_DIR/301.pub" "$PUBLIC_KEYS_DIR/311.pub"
if ( ensure_management_public_key_registry ) >/dev/null 2>&1; then
    fail "duplicate fingerprint was accepted"
fi
rm -f "$PUBLIC_KEYS_DIR/311.pub"

ssh-keygen -q -t ed25519 -N '' -C 'foreign-deployer' -f "$TMP/foreign"
cp "$TMP/foreign.pub" "$PVE_DEPLOYER_REGISTRY_KEY"
if ( ensure_management_public_key_registry ) >/dev/null 2>&1; then
    fail "mismatched pve_deployer_ed25519.pub was accepted"
fi

printf 'Management key registry tests passed.\n'

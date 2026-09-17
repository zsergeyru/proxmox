#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"
COMMON="$ROOT/scripts/pve/setup/lib/00-common.sh"
SYSTEM="$ROOT/scripts/pve/setup/lib/20-system.sh"
RUNTIME="$ROOT/scripts/pve/setup/lib/40-runtime.sh"

fail() {
    printf 'Deploy runtime contract test failed: %s\n' "$*" >&2
    exit 1
}

grep -Eq '^PVE_CONFIGURATION_VERSION=23$' "$COMMON" \
    || fail "PVE_CONFIGURATION_VERSION must be 23"

if grep -Eq 'BOOTSTRAP_KEY_FILE|PVE_BOOTSTRAP_KEY_FILE|BOOTSTRAP_KNOWN_HOSTS|CANONICAL_KEY_DIFFERS_FROM_BOOTSTRAP' "$COMMON" "$RUNTIME"; then
    fail "temporary GitHub Deploy Key contract must not remain in PVE Configuration"
fi

grep -q 'Постоянный GitHub Deploy Key .* отсутствует. Public Bootstrap должен создать его' "$RUNTIME" \
    || fail "PVE Configuration must require the permanent GitHub Deploy Key created by Public Bootstrap"

grep -q 'python3-jsonschema' "$SYSTEM" \
    || fail "PVE Configuration must install python3-jsonschema"
grep -q 'from jsonschema import Draft202012Validator' "$SYSTEM" \
    || fail "PVE Configuration must verify Draft202012Validator import"

grep -q 'deploy_ssh_dir="/var/lib/pvedeploy/.ssh"' "$RUNTIME" \
    || fail "pvedeploy SSH directory contract is missing"
grep -q 'guest_known_hosts="${deploy_ssh_dir}/known_hosts"' "$RUNTIME" \
    || fail "guest known_hosts contract is missing"
grep -q 'install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0700 "$deploy_ssh_dir"' "$RUNTIME" \
    || fail "pvedeploy .ssh must be mode 0700"
grep -q 'install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0600 /dev/null "$guest_known_hosts"' "$RUNTIME" \
    || fail "guest known_hosts must be created with mode 0600"
grep -q '\[\[ ! -L "$deploy_ssh_dir" \]\]' "$RUNTIME" \
    || fail "pvedeploy .ssh symlink guard is missing"
grep -q '\[\[ ! -L "$guest_known_hosts" \]\]' "$RUNTIME" \
    || fail "guest known_hosts symlink guard is missing"

printf 'Deploy runtime contract tests passed.\n'
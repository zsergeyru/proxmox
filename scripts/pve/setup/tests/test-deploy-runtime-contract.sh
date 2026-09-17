#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"
COMMON="$ROOT/scripts/pve/setup/lib/00-common.sh"
SYSTEM="$ROOT/scripts/pve/setup/lib/20-system.sh"
RUNTIME="$ROOT/scripts/pve/setup/lib/40-runtime.sh"
KEYS="$ROOT/scripts/pve/setup/lib/45-management-keys.sh"
TOOLING="$ROOT/scripts/pve/setup/lib/70-tooling.sh"
CONFIGURE="$ROOT/scripts/pve/setup/configure-pve.sh"

fail() {
    printf 'Deploy runtime contract test failed: %s\n' "$*" >&2
    exit 1
}

grep -Eq '^PVE_CONFIGURATION_VERSION=24$' "$COMMON" \
    || fail "PVE_CONFIGURATION_VERSION must be 24"

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

grep -q '^    45-management-keys.sh \\$' "$CONFIGURE" \
    || fail "PVE Configuration must source the management key registry module"
grep -q '^    ensure_management_public_key_registry$' "$CONFIGURE" \
    || fail "PVE Configuration must prepare the management key registry after deployer identity"
grep -q 'PUBLIC_KEYS_DIR="${RUNTIME_DIR}/public-keys"' "$KEYS" \
    || fail "canonical public-key registry path is missing"
grep -q 'DEPLOYER_REGISTRY_KEY="${PUBLIC_KEYS_DIR}/deployer.pub"' "$KEYS" \
    || fail "deployer.pub registry contract is missing"
grep -q 'MANAGEMENT_KEYS_AGGREGATE="${PUBLIC_KEYS_DIR}/management-authorized-keys"' "$KEYS" \
    || fail "management-authorized-keys aggregate contract is missing"
grep -q 'install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0750 "$PUBLIC_KEYS_DIR"' "$KEYS" \
    || fail "public-key registry must belong to pvedeploy with mode 0750"
grep -q 'Один management public key зарегистрирован дважды' "$KEYS" \
    || fail "registry duplicate fingerprint guard is missing"
grep -q 'автоматическая ротация запрещена' "$KEYS" \
    || fail "deployer registry mismatch must STOP instead of rotating"

grep -q '/usr/bin/python3 "\\$VALIDATOR"' "$TOOLING" \
    || fail "root wrapper must run the shared repository validator"
grep -q 'from guest_config import GuestConfigError, resolve_effective_guest' "$TOOLING" \
    || fail "root wrapper must use the shared guest resolver"
grep -q 'management.get("project_repo_read")' "$TOOLING" \
    || fail "root wrapper must derive project_repo_read from effective state"
grep -q 'exec 8<"\\$PROJECT_REPO_KEY"' "$TOOLING" \
    || fail "root wrapper must open the fixed Project Git key on a dedicated FD"
grep -q 'DEPLOY_GUEST_PROJECT_REPO_KEY_FD=8' "$TOOLING" \
    || fail "root wrapper must pass only the Project Git key FD number to deploy-guest"
if grep -q 'PROJECT_REPO_KEY=.*env' "$TOOLING"; then
    fail "Project Git private key path/material must not be passed through environment"
fi

printf 'Deploy runtime contract tests passed.\n'
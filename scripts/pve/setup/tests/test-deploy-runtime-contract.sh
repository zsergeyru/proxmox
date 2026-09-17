#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"
COMMON="$ROOT/scripts/pve/setup/lib/00-common.sh"
SYSTEM="$ROOT/scripts/pve/setup/lib/20-system.sh"
RUNTIME="$ROOT/scripts/pve/setup/lib/40-runtime.sh"
KEYS="$ROOT/scripts/pve/setup/lib/45-management-keys.sh"
TOOLING="$ROOT/scripts/pve/setup/lib/70-tooling.sh"
DEPLOY_GUEST="$ROOT/scripts/pve/deploy-guest.py"
CONFIGURE="$ROOT/scripts/pve/setup/configure-pve.sh"
PREFLIGHT="$ROOT/scripts/pve/setup/lib/10-preflight.sh"

fail() {
    printf 'Deploy runtime contract test failed: %s\n' "$*" >&2
    exit 1
}

grep -Eq '^PVE_CONFIGURATION_VERSION=30$' "$COMMON" \
    || fail "PVE_CONFIGURATION_VERSION must be 30"

if grep -Eq 'BOOTSTRAP_KEY_FILE|PVE_BOOTSTRAP_KEY_FILE|BOOTSTRAP_KNOWN_HOSTS|CANONICAL_KEY_DIFFERS_FROM_BOOTSTRAP' "$COMMON" "$RUNTIME"; then
    fail "temporary GitHub Deploy Key contract must not remain in PVE Configuration"
fi

grep -q 'Постоянный GitHub Deploy Key .* отсутствует. Public Bootstrap должен создать его' "$RUNTIME" \
    || fail "PVE Configuration must require the permanent GitHub Deploy Key created by Public Bootstrap"

grep -q 'python3-jsonschema' "$SYSTEM" \
    || fail "PVE Configuration must install python3-jsonschema"

grep -Fq 'check_node_name_resolution()' "$PREFLIGHT" \
    || fail "PVE preflight must validate node-name resolution"
grep -Fq 'getent ahosts "$node"' "$PREFLIGHT" \
    || fail "node-name resolution check must use system resolver"
grep -Fq '    check_node_name_resolution' "$PREFLIGHT" \
    || fail "PVE preflight must invoke node-name resolution check"
grep -q 'from jsonschema import Draft202012Validator' "$SYSTEM" \
    || fail "PVE Configuration must verify Draft202012Validator import"

grep -Fq 'deploy_ssh_dir="/var/lib/pvedeploy/.ssh"' "$RUNTIME" \
    || fail "pvedeploy SSH directory contract is missing"
grep -Fq 'guest_known_hosts="${deploy_ssh_dir}/known_hosts"' "$RUNTIME" \
    || fail "guest known_hosts contract is missing"
grep -Fq 'install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0700 "$deploy_ssh_dir"' "$RUNTIME" \
    || fail "pvedeploy .ssh must be mode 0700"
grep -Fq 'install -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0600 /dev/null "$guest_known_hosts"' "$RUNTIME" \
    || fail "guest known_hosts must be created with mode 0600"
grep -Fq '[[ ! -L "$deploy_ssh_dir" ]]' "$RUNTIME" \
    || fail "pvedeploy .ssh symlink guard is missing"
grep -Fq '[[ ! -L "$guest_known_hosts" ]]' "$RUNTIME" \
    || fail "guest known_hosts symlink guard is missing"

grep -Fq '    45-management-keys.sh \' "$CONFIGURE" \
    || fail "PVE Configuration must source the management key registry module"
grep -Fq '    ensure_management_public_key_registry' "$CONFIGURE" \
    || fail "PVE Configuration must prepare the management key registry after deployer identity"
grep -Fq 'PUBLIC_KEYS_DIR="${RUNTIME_DIR}/public-keys"' "$KEYS" \
    || fail "canonical public-key registry path is missing"
grep -Fq 'DEPLOYER_REGISTRY_KEY="${PUBLIC_KEYS_DIR}/deployer.pub"' "$KEYS" \
    || fail "deployer.pub registry contract is missing"
grep -Fq 'MANAGEMENT_KEYS_AGGREGATE="${PUBLIC_KEYS_DIR}/management-authorized-keys"' "$KEYS" \
    || fail "management-authorized-keys aggregate contract is missing"
grep -Fq 'install -d -o "$DEPLOY_USER" -g "$DEPLOY_USER" -m 0750 "$PUBLIC_KEYS_DIR"' "$KEYS" \
    || fail "public-key registry must belong to pvedeploy with mode 0750"
grep -Fq 'Один management public key зарегистрирован дважды' "$KEYS" \
    || fail "registry duplicate fingerprint guard is missing"
grep -Fq 'автоматическая ротация запрещена' "$KEYS" \
    || fail "deployer registry mismatch must STOP instead of rotating"

grep -Fq '/usr/bin/python3 "\$VALIDATOR"' "$TOOLING" \
    || fail "root wrapper must run the shared repository validator"
[[ -f "$DEPLOY_GUEST" ]] \
    || fail "deploy-guest runtime source must exist"
grep -Fq 'DEPLOY_GUEST_SOURCE_REVISION' "$DEPLOY_GUEST" \
    || fail "deploy-guest runtime must enforce pinned source revision"
grep -Fq 'DEPLOY_GUEST_PROJECT_REPO_KEY_FD' "$DEPLOY_GUEST" \
    || fail "deploy-guest runtime must consume Project Git key only through dedicated FD"
grep -Fq 'StrictHostKeyChecking=yes' "$DEPLOY_GUEST" \
    || fail "deploy-guest must use strict SSH host-key verification after trust establishment"
if grep -Eq '(qm|pct|pvesh)' "$DEPLOY_GUEST"; then
    fail "deploy-guest runtime must not bypass PVE REST API through qm/pct/pvesh"
fi
grep -Fq 'from guest_config import GuestConfigError, resolve_effective_guest' "$TOOLING" \
    || fail "root wrapper must use the shared guest resolver"
grep -Fq 'management.get("project_repo_read")' "$TOOLING" \
    || fail "root wrapper must derive project_repo_read from effective state"
grep -Fq 'exec 8<"\$PROJECT_REPO_KEY"' "$TOOLING" \
    || fail "root wrapper must open the fixed Project Git key on a dedicated FD"
grep -Fq 'DEPLOY_GUEST_PROJECT_REPO_KEY_FD=8' "$TOOLING" \
    || fail "root wrapper must pass only the Project Git key FD number to deploy-guest"
if grep -Fq 'PROJECT_REPO_KEY=.*env' "$TOOLING"; then
    fail "Project Git private key path/material must not be passed through environment"
fi

printf 'Deploy runtime contract tests passed.\n'
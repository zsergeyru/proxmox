#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"
TMP="$(mktemp -d)"
OUT="${TMP}/source-trust.out"
trap 'rm -rf "$TMP"' EXIT

cp -a "$ROOT" "$TMP/repo"
chown -R root:root "$TMP"
chmod -R go-w "$TMP/repo"
chmod 0700 "$TMP"
# Earlier CI steps intentionally create ignored artefacts such as __pycache__.
# The fixture models a production-clean source checkout, so clean only this
# disposable copy before testing the trust boundary itself.
git -C "$TMP/repo" clean -ffdx >/dev/null

CONFIGURE="$TMP/repo/scripts/pve/setup/configure-pve.sh"

bash "$CONFIGURE" --help >/dev/null

expect_trust_failure() {
    local label=$1
    set +e
    bash "$CONFIGURE" --help >"$OUT" 2>&1
    local rc=$?
    set -e
    if (( rc == 0 )); then
        printf 'Expected source trust failure for: %s\n' "$label" >&2
        cat "$OUT" >&2
        exit 1
    fi
}

chmod 0770 "$TMP"
expect_trust_failure "group-writable source parent"
chmod 0700 "$TMP"

chmod g+w "$TMP/repo/scripts/pve/setup/lib/00-common.sh"
expect_trust_failure "group-writable source file"
chmod g-w "$TMP/repo/scripts/pve/setup/lib/00-common.sh"

chown nobody:nogroup "$TMP/repo/scripts/pve/setup/lib/00-common.sh"
expect_trust_failure "non-root-owned source file"
chown root:root "$TMP/repo/scripts/pve/setup/lib/00-common.sh"

bash "$CONFIGURE" --help >/dev/null
printf 'Source trust boundary tests passed.\n'

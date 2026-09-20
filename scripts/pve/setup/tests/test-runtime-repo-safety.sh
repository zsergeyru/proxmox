#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# shellcheck source=/dev/null
source "$ROOT/scripts/pve/setup/lib/00-common.sh"
# shellcheck source=/dev/null
source "$ROOT/scripts/pve/setup/lib/40-runtime.sh"

expect_state() {
    local expected=$1 label=$2 actual
    actual="$(canonical_repo_state)"
    [[ "$actual" == "$expected" ]] || {
        printf 'Expected repo state %s for %s, got %s\n' "$expected" "$label" "$actual" >&2
        exit 1
    }
}

REPO_DIR="$TMP/repo"
expect_state absent "absent path"

mkdir "$REPO_DIR"
expect_state empty "empty directory"

printf 'foreign data\n' >"$REPO_DIR/important.txt"
expect_state unsafe "non-empty non-git directory"
[[ -f "$REPO_DIR/important.txt" ]] || {
    printf 'Safety classification modified foreign data\n' >&2
    exit 1
}

rm "$REPO_DIR/important.txt"
mkdir "$REPO_DIR/.git"
expect_state checkout "ordinary checkout marker"

rmdir "$REPO_DIR/.git"
printf 'gitdir: /tmp/external-worktree\n' >"$REPO_DIR/.git"
expect_state unsafe ".git file/worktree"

rm "$REPO_DIR/.git"
rmdir "$REPO_DIR"
printf 'not a directory\n' >"$REPO_DIR"
expect_state unsafe "ordinary file"

rm "$REPO_DIR"
ln -s "$TMP/elsewhere" "$REPO_DIR"
expect_state unsafe "symlink"

if grep -Fq 'rm -rf "$REPO_DIR"' "$ROOT/scripts/pve/setup/lib/40-runtime.sh"; then
    printf 'Dangerous automatic REPO_DIR deletion remains in runtime\n' >&2
    exit 1
fi

printf 'Canonical repo safety tests passed.\n'

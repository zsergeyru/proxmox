#!/usr/bin/env bash

install_sync_management_keys_tooling() {
    log "Установка команды sync-management-keys"

    local source="${REPO_DIR}/scripts/pve/sync-management-keys.py"
    [[ -f "$source" ]] \
        || die "Не найден runtime source sync-management-keys: ${source}"

    cat >/usr/local/sbin/sync-management-keys <<EOF_SYNC
#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="${REPO_DIR}"
RUNTIME_DIR="${RUNTIME_DIR}"
STATE_DIR="${STATE_DIR}"
DEPLOY_USER="${DEPLOY_USER}"
LOCK_FILE="${LOCK_FILE}"
SOURCE="${source}"

fail() {
    printf 'ОШИБКА: %s\\n' "\$*" >&2
    exit 1
}

[[ \$EUID -eq 0 ]] || fail "sync-management-keys нужно запускать от root"
for cmd in flock runuser git find stat python3; do
    command -v "\$cmd" >/dev/null 2>&1 || fail "Не найдена обязательная команда: \$cmd"
done

[[ -d "\$REPO_DIR/.git" ]] || fail "Каноническая копия Git отсутствует: \$REPO_DIR"
[[ -f "\$SOURCE" ]] || fail "Не найден runtime source: \$SOURCE"

parent_owner="\$(stat -c '%U:%G' "\$RUNTIME_DIR" 2>/dev/null || true)"
parent_mode="\$(stat -c '%a' "\$RUNTIME_DIR" 2>/dev/null || true)"
[[ "\$parent_owner" == "root:\$DEPLOY_USER" && "\$parent_mode" == "750" ]] \\
    || fail "\$RUNTIME_DIR должен быть root:\$DEPLOY_USER 0750"

violation="\$(find "\$REPO_DIR" -xdev \\( -type f -o -type d \\) \\( ! -uid 0 -o -perm /022 \\) -print -quit 2>/dev/null || true)"
[[ -z "\$violation" ]] \\
    || fail "Каноническая копия Git не root-trusted: \$violation"

status="\$(git -c "safe.directory=\$REPO_DIR" -C "\$REPO_DIR" status --porcelain=v1 --untracked-files=all --ignored)" \\
    || fail "Не удалось проверить canonical checkout"
status="\$(printf '%s\n' "\$status" | grep -Ev '^!! .*(__pycache__/|\.py[co]$)' || true)"
[[ -z "\$status" ]] \\
    || fail "Canonical checkout содержит local drift; первый элемент: \$(head -n1 <<<"\$status")"

revision="\$(git -c "safe.directory=\$REPO_DIR" -C "\$REPO_DIR" rev-parse HEAD 2>/dev/null || true)"
recorded_revision="\$(cat "\$STATE_DIR/last-revision" 2>/dev/null || true)"
[[ "\$revision" =~ ^[0-9a-f]{40}\$ ]] || fail "Не удалось определить Git revision"
[[ "\$recorded_revision" == "\$revision" ]] \\
    || fail "Canonical checkout revision отличается от PVE Configuration state; сначала выполните PVE Configuration"

exec 9>"\$LOCK_FILE"
flock -n 9 || fail "Другой Public Bootstrap, PVE Configuration, deploy-guest или sync-management-keys уже выполняется"

set +e
runuser -u "\$DEPLOY_USER" -- env \\
    SYNC_MANAGEMENT_KEYS_SOURCE_REVISION="\$revision" \\
    /usr/bin/python3 "\$SOURCE" "\$@"
rc=\$?
set -e
exit "\$rc"
EOF_SYNC

    chown root:root /usr/local/sbin/sync-management-keys
    chmod 0700 /usr/local/sbin/sync-management-keys
    ok "Установлена root-only команда /usr/local/sbin/sync-management-keys"
}

from pathlib import Path

root = Path('.')
tooling = root / 'scripts/pve/setup/lib/70-tooling.sh'
text = tooling.read_text(encoding='utf-8')

old = '''    status="\\$(canonical_git -C "\\$REPO_DIR" status --porcelain=v1 --untracked-files=all)" \\
        || fail "Не удалось проверить состояние Git в \\$REPO_DIR"'''
new = '''    status="\\$(canonical_git -C "\\$REPO_DIR" status --porcelain=v1 --untracked-files=all --ignored)" \\
        || fail "Не удалось проверить состояние Git в \\$REPO_DIR"
    status="\\$(printf '%s\\n' "\\$status" | grep -Ev '^!! .*(__pycache__/|\\.py[co]$)' || true)"'''
if old not in text:
    raise SystemExit('missing clean-repo anchor')
text = text.replace(old, new, 1)

replacements = [
    (
        'PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 - "\\$REPO_DIR" "\\$vmid"',
        'PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/run/proxmox-deployer-disabled-pycache /usr/bin/python3 - "\\$REPO_DIR" "\\$vmid"',
    ),
    (
        'PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 "\\$VALIDATOR"',
        'PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/run/proxmox-deployer-disabled-pycache /usr/bin/python3 "\\$VALIDATOR"',
    ),
    (
        '        PYTHONDONTWRITEBYTECODE=1 \\\n        DEPLOY_GUEST_SOURCE_REVISION=',
        '        PYTHONDONTWRITEBYTECODE=1 \\\n        PYTHONPYCACHEPREFIX=/run/proxmox-deployer-disabled-pycache \\\n        DEPLOY_GUEST_SOURCE_REVISION=',
    ),
    (
        '    runuser -u "\\$DEPLOY_USER" -- env PYTHONDONTWRITEBYTECODE=1 \\\n        DEPLOY_GUEST_SOURCE_REVISION=',
        '    runuser -u "\\$DEPLOY_USER" -- env PYTHONDONTWRITEBYTECODE=1 \\\n        PYTHONPYCACHEPREFIX=/run/proxmox-deployer-disabled-pycache \\\n        DEPLOY_GUEST_SOURCE_REVISION=',
    ),
]
for old, new in replacements:
    if old not in text:
        raise SystemExit(f'missing tooling anchor: {old!r}')
    text = text.replace(old, new, 1)
tooling.write_text(text, encoding='utf-8')

test = root / 'scripts/pve/setup/tests/test-deploy-runtime-contract.sh'
text = test.read_text(encoding='utf-8')
old = '''grep -Fq 'PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 "\\$VALIDATOR"' "$TOOLING" \\
    || fail "root wrapper must run the shared repository validator without writing bytecode"
if grep -Fq -- '--untracked-files=all --ignored' "$TOOLING"; then
    fail "deploy wrapper must not treat gitignored runtime artifacts as repository dirt"
fi
grep -Fq 'status --porcelain=v1 --untracked-files=all)' "$TOOLING" \\
    || fail "deploy wrapper must still reject tracked and non-ignored untracked changes"
grep -Fq 'PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 - "\\$REPO_DIR" "\\$vmid"' "$TOOLING" \\
    || fail "effective-state resolver must not write __pycache__ into canonical checkout"
grep -Fq 'runuser -u "\\$DEPLOY_USER" -- env PYTHONDONTWRITEBYTECODE=1' "$TOOLING" \\
    || fail "deploy runtime must disable bytecode writes for pvedeploy"'''
new = '''grep -Fq 'PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/run/proxmox-deployer-disabled-pycache /usr/bin/python3 "\\$VALIDATOR"' "$TOOLING" \\
    || fail "root wrapper must run validator without reading/writing canonical __pycache__"
grep -Fq -- 'status --porcelain=v1 --untracked-files=all --ignored)' "$TOOLING" \\
    || fail "deploy wrapper must inspect ignored state too"
grep -Fq "grep -Ev '^!! .*(__pycache__/|\\\\.py[co]$)'" "$TOOLING" \\
    || fail "deploy wrapper may exempt only Python bytecode from strict dirty-state checks"
grep -Fq 'PYTHONDONTWRITEBYTECODE=1 PYTHONPYCACHEPREFIX=/run/proxmox-deployer-disabled-pycache /usr/bin/python3 - "\\$REPO_DIR" "\\$vmid"' "$TOOLING" \\
    || fail "effective-state resolver must bypass canonical __pycache__"
grep -Fq 'PYTHONPYCACHEPREFIX=/run/proxmox-deployer-disabled-pycache' "$TOOLING" \\
    || fail "deploy runtime must use a non-canonical pycache prefix"
grep -Fq 'runuser -u "\\$DEPLOY_USER" -- env PYTHONDONTWRITEBYTECODE=1' "$TOOLING" \\
    || fail "deploy runtime must disable bytecode writes for pvedeploy"'''
if old not in text:
    raise SystemExit('missing test refinement anchor')
text = text.replace(old, new, 1)
test.write_text(text, encoding='utf-8')

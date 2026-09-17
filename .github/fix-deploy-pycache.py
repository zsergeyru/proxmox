from pathlib import Path

root = Path('.')

tooling = root / 'scripts/pve/setup/lib/70-tooling.sh'
text = tooling.read_text(encoding='utf-8')

replacements = [
    (
        'status="\\$(canonical_git -C "\\$REPO_DIR" status --porcelain=v1 --untracked-files=all --ignored)"',
        'status="\\$(canonical_git -C "\\$REPO_DIR" status --porcelain=v1 --untracked-files=all)"',
    ),
    (
        '    /usr/bin/python3 - "\\$REPO_DIR" "\\$vmid" <<\'PY_EFFECTIVE\'',
        '    PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 - "\\$REPO_DIR" "\\$vmid" <<\'PY_EFFECTIVE\'',
    ),
    (
        '/usr/bin/python3 "\\$VALIDATOR" || fail "Repository validation перед deploy-guest завершилась ошибкой"',
        'PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 "\\$VALIDATOR" || fail "Repository validation перед deploy-guest завершилась ошибкой"',
    ),
    (
        '        DEPLOY_GUEST_SOURCE_REVISION="\\$revision" \\\\\n        DEPLOY_GUEST_PROJECT_REPO_KEY_FD=8 \\\\\n        /usr/bin/python3 "\\$SOURCE" "\\$@"',
        '        PYTHONDONTWRITEBYTECODE=1 \\\\\n        DEPLOY_GUEST_SOURCE_REVISION="\\$revision" \\\\\n        DEPLOY_GUEST_PROJECT_REPO_KEY_FD=8 \\\\\n        /usr/bin/python3 "\\$SOURCE" "\\$@"',
    ),
    (
        '    runuser -u "\\$DEPLOY_USER" -- env DEPLOY_GUEST_SOURCE_REVISION="\\$revision" \\\\\n        /usr/bin/python3 "\\$SOURCE" "\\$@"',
        '    runuser -u "\\$DEPLOY_USER" -- env PYTHONDONTWRITEBYTECODE=1 \\\\\n        DEPLOY_GUEST_SOURCE_REVISION="\\$revision" \\\\\n        /usr/bin/python3 "\\$SOURCE" "\\$@"',
    ),
]

for old, new in replacements:
    if old not in text:
        raise SystemExit(f'missing tooling anchor: {old!r}')
    text = text.replace(old, new, 1)

tooling.write_text(text, encoding='utf-8')

common = root / 'scripts/pve/setup/lib/00-common.sh'
text = common.read_text(encoding='utf-8')
old = 'PVE_CONFIGURATION_VERSION=30'
new = 'PVE_CONFIGURATION_VERSION=31'
if old not in text:
    raise SystemExit('missing version anchor')
common.write_text(text.replace(old, new, 1), encoding='utf-8')

test = root / 'scripts/pve/setup/tests/test-deploy-runtime-contract.sh'
text = test.read_text(encoding='utf-8')
text = text.replace("grep -Eq '^PVE_CONFIGURATION_VERSION=30$' \"$COMMON\" \\\n    || fail \"PVE_CONFIGURATION_VERSION must be 30\"",
                    "grep -Eq '^PVE_CONFIGURATION_VERSION=31$' \"$COMMON\" \\\n    || fail \"PVE_CONFIGURATION_VERSION must be 31\"")
anchor = '''grep -Fq '/usr/bin/python3 "\\$VALIDATOR"' "$TOOLING" \\
    || fail "root wrapper must run the shared repository validator"'''
replacement = '''grep -Fq 'PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 "\\$VALIDATOR"' "$TOOLING" \\
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
if anchor not in text:
    raise SystemExit('missing runtime test anchor')
text = text.replace(anchor, replacement, 1)
test.write_text(text, encoding='utf-8')

from pathlib import Path

common = Path('scripts/pve/setup/lib/00-common.sh')
text = common.read_text(encoding='utf-8')
if 'PVE_CONFIGURATION_VERSION=28' not in text:
    raise SystemExit('PVE Configuration version 28 not found')
common.write_text(text.replace('PVE_CONFIGURATION_VERSION=28', 'PVE_CONFIGURATION_VERSION=29', 1), encoding='utf-8')

preflight = Path('scripts/pve/setup/lib/10-preflight.sh')
text = preflight.read_text(encoding='utf-8')
old_cmds = '        ip systemctl apt-get getent groupadd useradd; do'
new_cmds = '        ip systemctl apt-get getent hostname groupadd useradd; do'
if old_cmds not in text:
    raise SystemExit('preflight required-command anchor not found')
text = text.replace(old_cmds, new_cmds, 1)

function = """check_node_name_resolution() {
    local node resolved local_addresses addr nonlocal=\"\"

    node=\"$(hostname)\"
    [[ -n \"$node\" ]] || die \"Не удалось определить hostname PVE-ноды\"

    resolved=\"$(getent ahosts \"$node\" 2>/dev/null | awk '{print $1}' | sort -u)\"
    [[ -n \"$resolved\" ]] \\
        || die \"Имя PVE-ноды '${node}' не резолвится. Проверьте /etc/hosts или DNS до продолжения PVE Configuration.\"

    local_addresses=\"$(ip -o addr show up | awk '$3 == \\\"inet\\\" || $3 == \\\"inet6\\\" {sub(/\\\\/.*/, \\\"\\\", $4); print $4}' | sort -u)\"
    [[ -n \"$local_addresses\" ]] \\
        || die \"На PVE не найдено ни одного локального IP-адреса для проверки hostname '${node}'\"

    while IFS= read -r addr; do
        [[ -n \"$addr\" ]] || continue
        if ! grep -Fxq \"$addr\" <<<\"$local_addresses\"; then
            nonlocal+=\"${addr}\"$'\\n'
        fi
    done <<<\"$resolved\"

    if [[ -n \"$nonlocal\" ]]; then
        die \"Имя PVE-ноды '${node}' резолвится в нелокальный адрес(а): $(printf '%s' \"$nonlocal\" | paste -sd, -). Локальные адреса: $(printf '%s\\n' \"$local_addresses\" | paste -sd, -). Проверьте /etc/hosts или DNS.\"
    fi

    ok \"Hostname PVE-ноды ${node} резолвится только в локальный адрес(а): $(printf '%s\\n' \"$resolved\" | paste -sd, -)\"
}

"""
anchor = 'check_root_and_pve() {\n'
if anchor not in text:
    raise SystemExit('check_root_and_pve anchor not found')
text = text.replace(anchor, function + anchor, 1)
call_anchor = '    ok "Сетевой мост ${BRIDGE} найден"\n\n'
if call_anchor not in text:
    raise SystemExit('bridge call anchor not found')
text = text.replace(call_anchor, call_anchor + '    check_node_name_resolution\n\n', 1)
preflight.write_text(text, encoding='utf-8')

contract = Path('scripts/pve/setup/tests/test-deploy-runtime-contract.sh')
text = contract.read_text(encoding='utf-8')
if 'PVE_CONFIGURATION_VERSION=28' not in text or 'must be 28' not in text:
    raise SystemExit('runtime contract version 28 expectation not found')
text = text.replace('PVE_CONFIGURATION_VERSION=28', 'PVE_CONFIGURATION_VERSION=29', 1)
text = text.replace('PVE_CONFIGURATION_VERSION must be 28', 'PVE_CONFIGURATION_VERSION must be 29', 1)
anchor = 'CONFIGURE="$ROOT/scripts/pve/setup/configure-pve.sh"\n'
if anchor not in text:
    raise SystemExit('contract configure anchor not found')
text = text.replace(anchor, anchor + 'PREFLIGHT="$ROOT/scripts/pve/setup/lib/10-preflight.sh"\n', 1)
marker = "grep -q 'python3-jsonschema' \"$SYSTEM\" \\\n    || fail \"PVE Configuration must install python3-jsonschema\"\n"
checks = """
grep -Fq 'check_node_name_resolution()' "$PREFLIGHT" \\
    || fail "PVE preflight must validate node-name resolution"
grep -Fq 'getent ahosts "$node"' "$PREFLIGHT" \\
    || fail "node-name resolution check must use system resolver"
grep -Fq '    check_node_name_resolution' "$PREFLIGHT" \\
    || fail "PVE preflight must invoke node-name resolution check"
"""
if marker not in text:
    raise SystemExit('contract insertion marker not found')
text = text.replace(marker, marker + checks, 1)
contract.write_text(text, encoding='utf-8')

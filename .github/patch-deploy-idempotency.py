from pathlib import Path

p = Path("scripts/pve/deploy-guest.py")
text = p.read_text(encoding="utf-8")

anchor = '''def option_enabled(value: object) -> bool:
    """Parse PVE boolean option whose value may carry comma-separated suboptions."""
    return str(value or "").split(",", 1)[0].lower() in {"1", "true", "yes", "on"}


def update_kv_option'''
replacement = '''def option_enabled(value: object) -> bool:
    """Parse PVE boolean option whose value may carry comma-separated suboptions."""
    return str(value or "").split(",", 1)[0].lower() in {"1", "true", "yes", "on"}


def preserve_boolean_suboptions(value: object, enabled: bool) -> str:
    parts = [part for part in str(value or "").split(",") if part]
    tail = parts[1:] if parts else []
    return ",".join(["1" if enabled else "0", *tail])


def expected_lxc_features(desired: Desired, current: object) -> str:
    values: dict[str, str] = {}
    extras: list[str] = []
    for part in str(current or "").split(","):
        if not part:
            continue
        if "=" in part:
            key, value = part.split("=", 1)
            if key in {"keyctl", "nesting"}:
                values[key] = value
                continue
        extras.append(part)
    values["keyctl"] = str(int(bool(desired.effective["lxc"]["features"]["keyctl"])))
    values["nesting"] = str(int(bool(desired.effective["lxc"]["features"]["nesting"])))
    return ",".join([*extras, f"keyctl={values['keyctl']}", f"nesting={values['nesting']}"])


def update_kv_option'''
if anchor not in text:
    raise SystemExit("option helper anchor missing")
text = text.replace(anchor, replacement, 1)

old = '''        wanted_features = {
            f"keyctl={int(bool(e['lxc']['features']['keyctl']))}",
            f"nesting={int(bool(e['lxc']['features']['nesting']))}",
        }
        current_features = set(str(cfg.get("features") or "").split(","))
        items.append(
            PlanItem(
                "PVE features",
                "NO CHANGE" if wanted_features.issubset(current_features) else "UPDATE",
                "REQUIRES-STOP",
                ",".join(sorted(wanted_features)),
            )
        )
'''
new = '''        current_features = str(cfg.get("features") or "")
        target_features = expected_lxc_features(desired, current_features)
        items.append(
            PlanItem(
                "PVE features",
                "NO CHANGE" if set(filter(None, current_features.split(","))) == set(filter(None, target_features.split(","))) else "UPDATE",
                "REQUIRES-STOP",
                target_features,
            )
        )
'''
if old not in text:
    raise SystemExit("LXC features PLAN anchor missing")
text = text.replace(old, new, 1)

text = text.replace(
    '        "agent": 1 if e["vm"]["guest_agent"] else 0,',
    '        "agent": preserve_boolean_suboptions(cfg.get("agent"), bool(e["vm"]["guest_agent"])),',
    1,
)
text = text.replace(
    '        "features": f"keyctl={int(bool(e[\"lxc\"][\"features\"][\"keyctl\"]))},nesting={int(bool(e[\"lxc\"][\"features\"][\"nesting\"]))}",',
    '        "features": expected_lxc_features(desired, cfg.get("features")),',
    1,
)

old = '''def apt_install(address: str, packages: list[str]) -> None:
    args = " ".join(shlex.quote(item) for item in packages)
    script = f"""
set -eu
if dpkg --audit | grep -q .; then echo 'dpkg --audit reports unfinished state' >&2; exit 81; fi
if find /var/lib/dpkg/updates -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null | grep -q .; then echo 'dpkg updates directory is not empty' >&2; exit 82; fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends {args}
"""
    ssh_run(address, script, timeout=300)
'''
new = '''def apt_install(address: str, packages: list[str]) -> None:
    args = " ".join(shlex.quote(item) for item in packages)
    script = f"""
set -eu
if dpkg --audit | grep -q .; then echo 'dpkg --audit reports unfinished state' >&2; exit 81; fi
if find /var/lib/dpkg/updates -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null | grep -q .; then echo 'dpkg updates directory is not empty' >&2; exit 82; fi
missing=''
for pkg in {args}; do
    if ! dpkg-query -W -f='${{db:Status-Abbrev}}' "$pkg" 2>/dev/null | grep -q '^ii'; then
        missing="$missing $pkg"
    fi
done
[ -n "$missing" ] || exit 0
export DEBIAN_FRONTEND=noninteractive
apt-get update
# shellcheck disable=SC2086
apt-get install -y --no-install-recommends $missing
"""
    ssh_run(address, script, timeout=300)
'''
if old not in text:
    raise SystemExit("apt_install anchor missing")
text = text.replace(old, new, 1)

# VM source preflight: clone must preserve a usable primary NIC and root disk.
old = '''        if not isinstance(config, dict) or not boolish(config.get("template")):
            raise DeployError(f"VM source {template_vmid} не является template")
        return None
'''
new = '''        if not isinstance(config, dict) or not boolish(config.get("template")):
            raise DeployError(f"VM source {template_vmid} не является template")
        if not isinstance(config.get("net0"), str) or not config.get("net0"):
            raise DeployError(f"VM source {template_vmid} не содержит обязательный net0")
        if not isinstance(config.get("scsi0"), str) or not config.get("scsi0"):
            raise DeployError(f"VM source {template_vmid} не содержит обязательный scsi0")
        return None
'''
if old not in text:
    raise SystemExit("VM source verify anchor missing")
text = text.replace(old, new, 1)

p.write_text(text, encoding="utf-8")

# Extend unit coverage for preservation helpers.
test = Path("scripts/tests/test-deploy-guest.py")
t = test.read_text(encoding="utf-8")
anchor = '''    assert mod.expected_ipconfig(d) == "ip=192.168.3.1/16,gw=192.168.1.1"

    plan = mod.Plan(
'''
insert = '''    assert mod.expected_ipconfig(d) == "ip=192.168.3.1/16,gw=192.168.1.1"
    assert mod.preserve_boolean_suboptions("1,fstrim_cloned_disks=1", True) == "1,fstrim_cloned_disks=1"
    assert mod.preserve_boolean_suboptions("1,fstrim_cloned_disks=1", False) == "0,fstrim_cloned_disks=1"

    lxc = desired("lxc")
    lxc.effective["lxc"] = {"features": {"keyctl": True, "nesting": True}}
    assert set(mod.expected_lxc_features(lxc, "fuse=1,keyctl=0,nesting=1").split(",")) == {
        "fuse=1", "keyctl=1", "nesting=1"
    }

    plan = mod.Plan(
'''
if anchor not in t:
    raise SystemExit("deploy test helper anchor missing")
t = t.replace(anchor, insert, 1)
test.write_text(t, encoding="utf-8")

#!/usr/bin/env python3
"""Проверка guests/defaults.yaml и существующих guests/*/guest.yaml."""

from __future__ import annotations

import copy
import ipaddress
import re
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
GUESTS = ROOT / "guests"
DEFAULTS = GUESTS / "defaults.yaml"
SCHEMAS = {
    "source": ROOT / "schemas/guest.schema.yaml",
    "defaults": ROOT / "schemas/guest-defaults.schema.yaml",
    "effective": ROOT / "schemas/guest-effective.schema.yaml",
}
DIR_RE = re.compile(r"^(\d{3})-(.+)$")
LXC_RE = re.compile(r"^[A-Za-z0-9._-]+:vztmpl/[A-Za-z0-9._+-]+_amd64\.tar\.(?:zst|gz|xz)$")
BAD_SOURCE = ("<", ">", "*", "?", "13.x", "latest", "tbd", "todo")
UNRESOLVED = ("todo", "tbd", "pending", "unknown", "не определено", "не определён",
              "не определен", "не решено", "требует уточнения")
SECRET_KEYS = {"password", "passwd", "secret", "token", "token_secret",
               "private_key", "preshared_key"}
PRIVATE_KEY_MARKERS = ("-----BEGIN PRIVATE KEY-----", "-----BEGIN OPENSSH PRIVATE KEY-----",
                       "-----BEGIN RSA PRIVATE KEY-----", "-----BEGIN EC PRIVATE KEY-----")
SELF_MANAGED = {100, 301, 320, 9000}
OVERRIDE_PATHS = {
    ("node",), ("resources", "disk", "storage"), ("network", "bridge"),
    ("management", "ssh", "user"), ("management", "ssh", "port"),
}
MISSING = object()
errors, warnings = [], []


def fail(msg): errors.append(msg)
def warn(msg): warnings.append(msg)


def load_yaml(path):
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"{path.relative_to(ROOT)}: не удалось прочитать YAML: {exc}")
        return None
    if not isinstance(data, dict):
        fail(f"{path.relative_to(ROOT)}: YAML должен содержать mapping/object")
        return None
    return data


def load_validator(path):
    try:
        schema = yaml.safe_load(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)
    except Exception as exc:
        fail(f"{path.relative_to(ROOT)}: некорректная schema валидатора: {exc}")
        return Draft202012Validator({})


def validate_schema(rel, data, validator, label):
    found = sorted(validator.iter_errors(data),
                   key=lambda e: (list(e.absolute_path), e.message))
    for err in found:
        where = ".".join(map(str, err.absolute_path)) or "<root>"
        fail(f"{rel}: ошибка {label} в {where}: {err.message}")
    return not found


def merge(base, overlay):
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def leaves(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from leaves(child, path + (str(key),))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            yield from leaves(child, path + (str(idx),))
    else:
        yield path, value


def keys(value, path=()):
    if isinstance(value, dict):
        for key, child in value.items():
            p = path + (str(key),)
            yield p, str(key)
            yield from keys(child, p)
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            yield from keys(child, path + (str(idx),))


def get_nested(data, path):
    value = data
    for part in path:
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def check_secrets(rel, data):
    for path, key in keys(data):
        if key.lower() in SECRET_KEYS:
            fail(f"{rel}: запрещено хранить секретоподобный ключ {'.'.join(path)}")
    for path, value in leaves(data):
        if isinstance(value, str) and any(m in value for m in PRIVATE_KEY_MARKERS):
            fail(f"{rel}: обнаружен приватный ключ в {'.'.join(path) or '<root>'}")


def network_defaults(defaults):
    rel = DEFAULTS.relative_to(ROOT)
    net = defaults.get("defaults", {}).get("network", {})
    try:
        subnet = ipaddress.ip_network(net.get("subnet"), strict=True)
    except ValueError as exc:
        fail(f"{rel}: некорректный defaults.network.subnet: {exc}")
        return None
    if not isinstance(subnet, ipaddress.IPv4Network):
        fail(f"{rel}: defaults.network.subnet должен быть IPv4")
        return None
    if subnet.prefixlen != 16:
        fail(f"{rel}: defaults.network.subnet должен быть /16 для VMID-адресации")
    try:
        gateway = ipaddress.ip_address(net.get("gateway"))
    except ValueError as exc:
        fail(f"{rel}: некорректный defaults.network.gateway: {exc}")
        return None
    if not isinstance(gateway, ipaddress.IPv4Address):
        fail(f"{rel}: defaults.network.gateway должен быть IPv4")
        return None
    if gateway not in subnet:
        fail(f"{rel}: gateway {gateway} находится вне {subnet}")
    if gateway in {subnet.network_address, subnet.broadcast_address}:
        fail(f"{rel}: gateway {gateway} не может быть network/broadcast")
    return {"subnet": subnet, "gateway": gateway}


def vmid_ip(vmid, cfg):
    a, b, _, _ = str(cfg["subnet"].network_address).split(".")
    text = f"{vmid:03d}"
    return ipaddress.ip_address(f"{a}.{b}.{int(text[0])}.{int(text[1:])}")


def parse_bare_ip(rel, field, value):
    if not isinstance(value, str):
        fail(f"{rel}: {field} должен быть строкой IPv4")
        return None
    if "/" in value:
        fail(f"{rel}: {field} должен быть без /prefix; маска берётся из defaults.yaml")
        return None
    try:
        ip = ipaddress.ip_address(value)
    except ValueError as exc:
        fail(f"{rel}: некорректный {field} {value!r}: {exc}")
        return None
    if not isinstance(ip, ipaddress.IPv4Address):
        fail(f"{rel}: {field} должен быть IPv4")
        return None
    return ip


def check_address(rel, vmid, ip, cfg, used):
    subnet, gateway = cfg["subnet"], cfg["gateway"]
    if ip not in subnet:
        fail(f"{rel}: management IP {ip} находится вне подсети {subnet}")
        return
    if ip in {subnet.network_address, subnet.broadcast_address}:
        fail(f"{rel}: management IP {ip} не может быть network/broadcast")
    if ip == gateway:
        fail(f"{rel}: management IP {ip} не может совпадать с gateway")
    if ip in used:
        fail(f"дублирующийся статический IP {ip}: {used[ip]} и {rel}")
    else:
        used[ip] = rel
    expected = vmid_ip(vmid, cfg)
    if ip != expected:
        warn(f"{rel}: management IP {ip} не соответствует правилу VMID {vmid}; ожидается {expected}")


def resolve_effective(rel, source, defaults, cfg):
    profile = defaults.get("profiles", {}).get(source.get("profile"))
    if not isinstance(profile, dict):
        fail(f"{rel}: неизвестный профиль {source.get('profile')!r}")
        return None
    effective = merge(merge(defaults.get("defaults", {}), profile), source)
    override = get_nested(source, ("network", "ipv4", "address"))
    ip = vmid_ip(source["vmid"], cfg) if override is MISSING else parse_bare_ip(
        rel, "network.ipv4.address", override)
    if ip is None:
        return None
    effective.setdefault("network", {})["ipv4"] = {
        "address": f"{ip}/{cfg['subnet'].prefixlen}"
    }
    return effective


def check_source_overrides(rel, source, defaults):
    if not source.get("deployable"):
        return
    profile = defaults.get("profiles", {}).get(source.get("profile"))
    if not isinstance(profile, dict):
        return
    inherited = merge(defaults.get("defaults", {}), profile)
    for path, value in leaves(source):
        if path == ("network", "ipv4", "address"):
            continue
        old = get_nested(inherited, path)
        if old is MISSING:
            continue
        dotted = ".".join(path)
        if value == old:
            warn(f"{rel}: избыточное переопределение {dotted}={value!r}; значение уже наследуется")
        elif path in OVERRIDE_PATHS:
            warn(f"{rel}: {dotted} переопределяет проектное значение {old!r} на {value!r}")


def check_effective_network(rel, source, effective, cfg, used):
    net = effective["network"]
    try:
        iface = ipaddress.ip_interface(net["ipv4"]["address"])
        eff_subnet = ipaddress.ip_network(net["subnet"], strict=True)
        eff_gateway = ipaddress.ip_address(net["gateway"])
    except ValueError as exc:
        fail(f"{rel}: некорректная effective network: {exc}")
        return
    if not isinstance(iface, ipaddress.IPv4Interface):
        fail(f"{rel}: effective network.ipv4.address должен быть IPv4")
        return
    if iface.network.prefixlen != cfg["subnet"].prefixlen:
        fail(f"{rel}: effective IP должен использовать /{cfg['subnet'].prefixlen}")
    if eff_subnet != cfg["subnet"]:
        fail(f"{rel}: effective subnet должен приходить из guests/defaults.yaml")
    if eff_gateway != cfg["gateway"]:
        fail(f"{rel}: effective gateway должен приходить из guests/defaults.yaml")
    check_address(rel, source["vmid"], iface.ip, cfg, used)

    override = get_nested(source, ("network", "ipv4", "address"))
    if override is not MISSING and iface.ip == vmid_ip(source["vmid"], cfg):
        warn(f"{rel}: network.ipv4.address={iface.ip} совпадает с вычисляемым IP; override избыточен")


def check_resources(rel, data):
    res = data.get("resources", {})
    mem, balloon = res.get("memory_mb"), res.get("ballooning_mb")
    if isinstance(mem, int) and isinstance(balloon, int) and balloon > mem:
        fail(f"{rel}: ballooning_mb ({balloon}) не может превышать memory_mb ({mem})")


def check_state(rel, source, effective):
    state, deployable = source.get("state"), source.get("deployable")
    if deployable and state in {"bootstrap", "legacy"}:
        warn(f"{rel}: state={state!r} используется вместе с deployable: true")
    if deployable:
        text = str(source.get("description", "")).lower()
        marker = next((m for m in UNRESOLVED if m in text), None)
        if marker:
            warn(f"{rel}: deployable-гость содержит в description маркер незавершённости {marker!r}")
    boot = effective.get("boot", {})
    if state == "active" and boot.get("onboot") is False:
        warn(f"{rel}: активный объект имеет boot.onboot=false")
    pool = effective.get("placement", {}).get("pool")
    if deployable and effective.get("protection") is True and pool == "managed":
        warn(f"{rel}: protection=true у гостя в pool 'managed'")


def check_profiles(defaults):
    rel = DEFAULTS.relative_to(ROOT)
    for name, profile in defaults.get("profiles", {}).items():
        if not isinstance(profile, dict):
            continue
        if "network" in profile:
            fail(f"{rel}: профиль {name!r} не должен переопределять defaults.network")
        if profile.get("type") == "vm" and profile.get("vm", {}).get("guest_agent") is not True:
            fail(f"{rel}: VM-профиль {name!r} должен включать QEMU guest agent")
        if profile.get("type") == "lxc":
            lxc = profile.get("lxc", {})
            tmpl = lxc.get("source", {}).get("ostemplate", "")
            if any(m in tmpl.lower() for m in BAD_SOURCE) or not LXC_RE.fullmatch(tmpl):
                fail(f"{rel}: профиль {name!r} должен использовать pinned Proxmox vztmpl")
            if lxc.get("container_runtime") == "docker":
                f = lxc.get("features", {})
                if lxc.get("unprivileged") is not True or f.get("nesting") is not True or f.get("keyctl") is not True:
                    fail(f"{rel}: Docker-профиль {name!r} должен быть unprivileged + nesting + keyctl")


def check_deployable(rel, source, effective):
    for key in ("type", "vm", "lxc"):
        if key in source:
            fail(f"{rel}: deployable-гость должен получать {key!r} из profile")
    vmid, kind = effective["vmid"], effective["type"]
    pool = effective["placement"]["pool"]
    if vmid in SELF_MANAGED:
        if pool is not None:
            fail(f"{rel}: VMID {vmid} не должен находиться в обычном managed pool")
    elif pool != "managed":
        fail(f"{rel}: обычный deployable-гость должен использовать pool 'managed'")
    ssh = effective["management"]["ssh"]
    if ssh != {"user": "ops", "port": 22}:
        fail(f"{rel}: effective management.ssh должен быть ops:22")
    if kind == "vm":
        src = effective["vm"]["source"]
        if src["template_vmid"] == vmid:
            fail(f"{rel}: VM не может клонироваться сама из себя")
        if effective["vm"]["guest_agent"] is not True:
            fail(f"{rel}: deployable VM должна включать QEMU guest agent")
    elif kind == "lxc":
        tmpl = effective["lxc"]["source"]["ostemplate"]
        if any(m in tmpl.lower() for m in BAD_SOURCE) or not LXC_RE.fullmatch(tmpl):
            fail(f"{rel}: lxc.source.ostemplate должен быть pinned Proxmox vztmpl")


def manifests():
    return sorted(p for p in GUESTS.glob("*/guest.yaml") if DIR_RE.match(p.parent.name))


def validate():
    validators = {k: load_validator(v) for k, v in SCHEMAS.items()}
    defaults = load_yaml(DEFAULTS)
    if defaults is None:
        return
    rel_defaults = DEFAULTS.relative_to(ROOT)
    check_secrets(rel_defaults, defaults)
    ok = validate_schema(rel_defaults, defaults, validators["defaults"], "defaults schema")
    cfg = network_defaults(defaults)
    check_profiles(defaults)
    if not ok or cfg is None:
        return

    seen, used_ips, used_profiles = {}, {}, set()
    profiles = defaults.get("profiles", {})

    for path in manifests():
        rel = path.relative_to(ROOT)
        match = DIR_RE.match(path.parent.name)
        data = load_yaml(path)
        if data is None or not validate_schema(rel, data, validators["source"], "source schema"):
            continue
        vmid, expected_vmid, expected_name = data["vmid"], int(match.group(1)), match.group(2)
        if vmid != expected_vmid:
            fail(f"{rel}: vmid {vmid} не соответствует имени каталога {expected_vmid}")
        if data["name"] != expected_name:
            fail(f"{rel}: name {data['name']!r} не соответствует имени каталога {expected_name!r}")
        if vmid in seen:
            fail(f"дублирующийся VMID {vmid}: {seen[vmid]} и {rel}")
        else:
            seen[vmid] = rel
        check_secrets(rel, data)

        if data["deployable"]:
            if data.get("profile") in profiles:
                used_profiles.add(data["profile"])
            check_source_overrides(rel, data, defaults)
            effective = resolve_effective(rel, data, defaults, cfg)
            if effective is None or not validate_schema(rel, effective, validators["effective"], "effective schema"):
                continue
            check_resources(rel, effective)
            check_effective_network(rel, data, effective, cfg, used_ips)
            check_deployable(rel, data, effective)
            check_state(rel, data, effective)
        else:
            check_resources(rel, data)
            override = get_nested(data, ("network", "ipv4", "address"))
            if override is not MISSING:
                ip = parse_bare_ip(rel, "network.ipv4.address", override)
                if ip is not None:
                    check_address(rel, vmid, ip, cfg, used_ips)
            check_state(rel, data, data)

    for profile in sorted(set(profiles) - used_profiles):
        warn(f"{rel_defaults}: профиль {profile!r} не используется ни одним deployable-гостем")


def main():
    validate()
    unique = list(dict.fromkeys(warnings))
    if unique:
        print("Предупреждения проверки guest-конфигурации:")
        for msg in unique:
            print(f" - {msg}")
    if errors:
        print("Ошибки проверки guest-конфигурации:")
        for msg in errors:
            print(f" - {msg}")
        return 1
    print("Проверка guest-конфигурации пройдена.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

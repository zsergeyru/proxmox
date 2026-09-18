#!/usr/bin/env python3
"""Проверка guests/defaults.yaml и существующих guests/*/guest.yaml."""

from __future__ import annotations

import ipaddress
import re
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from guest_config import (
    MISSING,
    GuestConfigError,
    NetworkConfig,
    deep_merge,
    get_nested,
    parse_bare_ipv4,
    parse_network_config,
    resolve_effective_guest,
    vmid_address,
)

ROOT = Path(__file__).resolve().parents[1]
GUESTS = ROOT / "guests"
DEFAULTS = GUESTS / "defaults.yaml"
SCHEMAS = {
    "source": ROOT / "schemas/guest.schema.yaml",
    "defaults": ROOT / "schemas/guest-defaults.schema.yaml",
    "effective": ROOT / "schemas/guest-effective.schema.yaml",
}
DIR_RE = re.compile(r"^(\d{3})-(.+)$")
LXC_SELECTOR_RE = re.compile(
    r"^[A-Za-z0-9._-]+:vztmpl/[A-Za-z0-9][A-Za-z0-9._+-]*$"
)
LXC_ARCHIVE_RE = re.compile(r"_amd64\.tar\.(?:zst|gz|xz)$")
BAD_SOURCE = ("<", ">", "*", "?", "13.x", "latest", "tbd", "todo")
UNRESOLVED = (
    "todo",
    "tbd",
    "pending",
    "unknown",
    "не определено",
    "не определён",
    "не определен",
    "не решено",
    "требует уточнения",
)
SECRET_KEYS = {
    "password",
    "passwd",
    "secret",
    "token",
    "token_secret",
    "private_key",
    "preshared_key",
}
PRIVATE_KEY_MARKERS = (
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
)
SELF_MANAGED = {100, 301, 320, 9000}
OVERRIDE_PATHS = {
    ("node",),
    ("placement", "pool"),
    ("resources", "disk_storage"),
}
errors: list[str] = []
warnings: list[str] = []


def fail(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def load_yaml(path: Path) -> dict | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"{path.relative_to(ROOT)}: не удалось прочитать YAML: {exc}")
        return None
    if not isinstance(data, dict):
        fail(f"{path.relative_to(ROOT)}: YAML должен содержать mapping/object")
        return None
    return data


def load_validator(path: Path) -> Draft202012Validator:
    try:
        schema = yaml.safe_load(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema)
    except Exception as exc:
        fail(f"{path.relative_to(ROOT)}: некорректная schema валидатора: {exc}")
        return Draft202012Validator({})


def validate_schema(
    rel: Path,
    data: dict,
    validator: Draft202012Validator,
    label: str,
) -> bool:
    found = sorted(
        validator.iter_errors(data),
        key=lambda err: (list(err.absolute_path), err.message),
    )
    for err in found:
        where = ".".join(map(str, err.absolute_path)) or "<root>"
        fail(f"{rel}: ошибка {label} в {where}: {err.message}")
    return not found


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
            current = path + (str(key),)
            yield current, str(key)
            yield from keys(child, current)
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            yield from keys(child, path + (str(idx),))


def check_secrets(rel: Path, data: dict) -> None:
    for path, key in keys(data):
        if key.lower() in SECRET_KEYS:
            fail(f"{rel}: запрещено хранить секретоподобный ключ {'.'.join(path)}")
    for path, value in leaves(data):
        if isinstance(value, str) and any(marker in value for marker in PRIVATE_KEY_MARKERS):
            fail(f"{rel}: обнаружен приватный ключ в {'.'.join(path) or '<root>'}")


def network_defaults(defaults: dict) -> NetworkConfig | None:
    try:
        return parse_network_config(defaults)
    except GuestConfigError as exc:
        fail(f"{DEFAULTS.relative_to(ROOT)}: {exc}")
        return None


def check_address(
    rel: Path,
    vmid: int,
    address: ipaddress.IPv4Address,
    cfg: NetworkConfig,
    used: dict[ipaddress.IPv4Address, Path],
) -> None:
    subnet, gateway = cfg.subnet, cfg.gateway
    if address not in subnet:
        fail(f"{rel}: management IP {address} находится вне подсети {subnet}")
        return
    if address in {subnet.network_address, subnet.broadcast_address}:
        fail(f"{rel}: management IP {address} не может быть network/broadcast")
    if address == gateway:
        fail(f"{rel}: management IP {address} не может совпадать с gateway")
    if address in used:
        fail(f"дублирующийся статический IP {address}: {used[address]} и {rel}")
    else:
        used[address] = rel
    expected = vmid_address(vmid, cfg)
    if address != expected:
        warn(
            f"{rel}: management IP {address} не соответствует правилу VMID {vmid}; "
            f"ожидается {expected}"
        )


def check_source_overrides(rel: Path, source: dict, defaults: dict) -> None:
    if not source.get("profile"):
        return
    profile = defaults.get("profiles", {}).get(source.get("profile"))
    if not isinstance(profile, dict):
        return

    inherited = deep_merge(defaults.get("defaults", {}), profile)
    for path, value in leaves(source):
        if path == ("network", "ipv4"):
            continue
        old = get_nested(inherited, path)
        if old is MISSING:
            continue
        dotted = ".".join(path)
        if value == old:
            warn(
                f"{rel}: избыточное переопределение {dotted}={value!r}; "
                "значение уже наследуется"
            )
        elif path in OVERRIDE_PATHS:
            warn(
                f"{rel}: {dotted} переопределяет проектное значение "
                f"{old!r} на {value!r}"
            )


def check_effective_network(
    rel: Path,
    source: dict,
    effective: dict,
    cfg: NetworkConfig,
    used: dict[ipaddress.IPv4Address, Path],
) -> None:
    net = effective["network"]
    try:
        iface = ipaddress.ip_interface(net["ipv4"])
        effective_subnet = ipaddress.ip_network(net["subnet"], strict=True)
        effective_gateway = ipaddress.ip_address(net["gateway"])
    except (TypeError, ValueError) as exc:
        fail(f"{rel}: некорректная effective network: {exc}")
        return

    if not isinstance(iface, ipaddress.IPv4Interface):
        fail(f"{rel}: effective network.ipv4 должен быть IPv4")
        return
    if iface.network.prefixlen != cfg.subnet.prefixlen:
        fail(f"{rel}: effective IP должен использовать /{cfg.subnet.prefixlen}")
    if effective_subnet != cfg.subnet:
        fail(f"{rel}: effective subnet должен приходить из guests/defaults.yaml")
    if effective_gateway != cfg.gateway:
        fail(f"{rel}: effective gateway должен приходить из guests/defaults.yaml")

    check_address(rel, source["vmid"], iface.ip, cfg, used)

    override = get_nested(source, ("network", "ipv4"))
    if override is not MISSING and iface.ip == vmid_address(source["vmid"], cfg):
        warn(
            f"{rel}: network.ipv4={iface.ip} совпадает с вычисляемым IP; "
            "override избыточен"
        )


def check_resources(rel: Path, data: dict, kind: str | None = None) -> None:
    resources = data.get("resources", {})
    if not isinstance(resources, dict):
        return
    memory = resources.get("memory_mb")
    ballooning = resources.get("ballooning_mb")
    if isinstance(memory, int) and isinstance(ballooning, int) and ballooning > memory:
        fail(
            f"{rel}: ballooning_mb ({ballooning}) не может превышать "
            f"memory_mb ({memory})"
        )
    if kind == "vm" and "swap_mb" in resources:
        fail(f"{rel}: resources.swap_mb разрешён только для LXC")
    if kind == "lxc" and "ballooning_mb" in resources:
        fail(f"{rel}: resources.ballooning_mb разрешён только для VM")


def check_description(rel: Path, source: dict) -> None:
    if not source.get("profile"):
        return
    text = str(source.get("description", "")).lower()
    marker = next((item for item in UNRESOLVED if item in text), None)
    if marker:
        warn(
            f"{rel}: гость с profile содержит в description "
            f"маркер незавершённости {marker!r}"
        )


def valid_lxc_selector(value: object) -> bool:
    if not isinstance(value, str):
        return False
    lower = value.lower()
    return (
        not any(marker in lower for marker in BAD_SOURCE)
        and LXC_SELECTOR_RE.fullmatch(value) is not None
        and LXC_ARCHIVE_RE.search(value) is None
    )


def check_profiles(defaults: dict) -> None:
    rel = DEFAULTS.relative_to(ROOT)
    for name, profile in defaults.get("profiles", {}).items():
        if not isinstance(profile, dict):
            continue
        if profile.get("type") == "lxc" and not valid_lxc_selector(profile.get("ostemplate")):
            fail(
                f"{rel}: профиль {name!r} должен использовать "
                "селектор семейства Proxmox vztmpl без версии и имени архива"
            )


def check_managed_guest(rel: Path, source: dict, effective: dict) -> None:
    vmid, kind = effective["vmid"], effective["type"]
    pool = effective["placement"]["pool"]
    if vmid in SELF_MANAGED:
        if pool is not None:
            fail(f"{rel}: VMID {vmid} не должен находиться в обычном managed pool")
    elif pool != "managed":
        fail(f"{rel}: обычный гость с profile должен использовать pool 'managed'")
    if effective.get("protection") is True and pool == "managed":
        warn(f"{rel}: protection=true у гостя в pool 'managed'")

    if kind == "vm":
        if effective["template_vmid"] == vmid:
            fail(f"{rel}: VM не может клонироваться сама из себя")
    elif kind == "lxc":
        selector = effective["ostemplate"]
        if not valid_lxc_selector(selector):
            fail(
                f"{rel}: ostemplate должен быть селектором семейства "
                "Proxmox vztmpl без версии и имени архива"
            )
        bootstrap = effective.get("bootstrap", [])
        features = effective.get("features", [])
        if "docker" in bootstrap and "container-host" not in features:
            fail(
                f"{rel}: bootstrap 'docker' для LXC требует "
                "profile feature 'container-host'"
            )


def manifests() -> list[Path]:
    found: list[Path] = []
    for path in sorted(GUESTS.glob("*/guest.yaml")):
        if not DIR_RE.fullmatch(path.parent.name):
            fail(
                f"{path.relative_to(ROOT)}: каталог гостя должен иметь имя "
                "NNN-name (ровно три цифры VMID, дефис и непустое имя)"
            )
            continue
        found.append(path)
    return found


def validate() -> None:
    validators = {key: load_validator(path) for key, path in SCHEMAS.items()}
    defaults = load_yaml(DEFAULTS)
    if defaults is None:
        return

    rel_defaults = DEFAULTS.relative_to(ROOT)
    check_secrets(rel_defaults, defaults)
    defaults_ok = validate_schema(
        rel_defaults,
        defaults,
        validators["defaults"],
        "defaults schema",
    )
    cfg = network_defaults(defaults)
    check_profiles(defaults)
    if not defaults_ok or cfg is None:
        return

    seen: dict[int, Path] = {}
    used_ips: dict[ipaddress.IPv4Address, Path] = {}
    used_profiles: set[str] = set()
    profiles = defaults.get("profiles", {})

    for path in manifests():
        rel = path.relative_to(ROOT)
        match = DIR_RE.fullmatch(path.parent.name)
        assert match is not None

        data = load_yaml(path)
        if data is None or not validate_schema(
            rel,
            data,
            validators["source"],
            "source schema",
        ):
            continue

        vmid = data["vmid"]
        expected_vmid = int(match.group(1))
        expected_name = match.group(2)
        if vmid != expected_vmid:
            fail(f"{rel}: vmid {vmid} не соответствует имени каталога {expected_vmid}")
        if data["name"] != expected_name:
            fail(
                f"{rel}: name {data['name']!r} не соответствует "
                f"имени каталога {expected_name!r}"
            )
        if vmid in seen:
            fail(f"дублирующийся VMID {vmid}: {seen[vmid]} и {rel}")
        else:
            seen[vmid] = rel
        check_secrets(rel, data)

        if data.get("profile"):
            if data.get("profile") in profiles:
                used_profiles.add(data["profile"])
            check_source_overrides(rel, data, defaults)
            try:
                resolved = resolve_effective_guest(data, defaults, cfg)
            except GuestConfigError as exc:
                fail(f"{rel}: {exc}")
                continue

            effective = resolved.effective
            check_resources(rel, effective, str(effective.get("type")))
            if not validate_schema(
                rel,
                effective,
                validators["effective"],
                "effective schema",
            ):
                continue
            check_effective_network(rel, data, effective, cfg, used_ips)
            check_managed_guest(rel, data, effective)
            check_description(rel, data)
        else:
            check_resources(rel, data)
            override = get_nested(data, ("network", "ipv4"))
            if override is not MISSING:
                try:
                    address = parse_bare_ipv4(override)
                except GuestConfigError as exc:
                    fail(f"{rel}: {exc}")
                else:
                    check_address(rel, vmid, address, cfg, used_ips)
            check_description(rel, data)

    for profile in sorted(set(profiles) - used_profiles):
        warn(
            f"{rel_defaults}: профиль {profile!r} не используется "
            "ни одним гостем с profile"
        )


def main() -> int:
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

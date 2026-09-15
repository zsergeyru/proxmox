#!/usr/bin/env python3
"""Consistency and deploy-readiness checks for the Proxmox infrastructure repository."""

from __future__ import annotations

import copy
import ipaddress
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
GUESTS = ROOT / "guests"
GUEST_DEFAULTS = GUESTS / "defaults.yaml"
VMID_PLAN = ROOT / "docs" / "11-vmid-plan.md"
GUEST_SCHEMA = ROOT / "schemas" / "guest.schema.yaml"
GUEST_DEFAULTS_SCHEMA = ROOT / "schemas" / "guest-defaults.schema.yaml"
GUEST_EFFECTIVE_SCHEMA = ROOT / "schemas" / "guest-effective.schema.yaml"

GUEST_DIR_RE = re.compile(r"^(\d{3})-(.+)$")
PLAN_VMID_RE = re.compile(r"^- `(?P<vmid>\d{3})`\s+—", re.MULTILINE)
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
LXC_OSTEMPLATE_RE = re.compile(
    r"^[A-Za-z0-9._-]+:vztmpl/[A-Za-z0-9._+-]+_amd64\.tar\.(?:zst|gz|xz)$"
)
FORBIDDEN_SOURCE_MARKERS = ("<", ">", "*", "?", "13.x", "latest", "tbd", "todo")
UNRESOLVED_DESCRIPTION_MARKERS = (
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
FORBIDDEN_MANIFEST_KEYS = {
    "password",
    "passwd",
    "secret",
    "token",
    "token_secret",
    "private_key",
    "preshared_key",
}
SELF_MANAGED_EXCLUSIONS = {100, 301, 320, 9000}
PROJECT_DEFAULT_OVERRIDE_PATHS = {
    ("node",),
    ("resources", "disk", "storage"),
    ("network", "bridge"),
    ("management", "ssh", "user"),
    ("management", "ssh", "port"),
}
NETWORK_MIGRATION_PATHS = {
    ("network", "mode"),
    ("network", "default_gateway"),
}
PRIVATE_KEY_MARKERS = tuple(
    "-----BEGIN " + key_type + "-----"
    for key_type in (
        "PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
    )
)
_MISSING = object()

errors: list[str] = []
warnings: list[str] = []


def fail(message: str) -> None:
    errors.append(message)


def warn(message: str) -> None:
    warnings.append(message)


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [ROOT / p.decode() for p in result.stdout.split(b"\0") if p]


def validate_yaml(files: list[Path]) -> None:
    for path in files:
        if path.suffix.lower() not in {".yaml", ".yml"}:
            continue
        try:
            yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            fail(f"invalid YAML: {path.relative_to(ROOT)}: {exc}")


def planned_vmids() -> set[int]:
    text = VMID_PLAN.read_text(encoding="utf-8")
    values = [int(match.group("vmid")) for match in PLAN_VMID_RE.finditer(text)]
    duplicates = sorted(vmid for vmid in set(values) if values.count(vmid) > 1)
    for vmid in duplicates:
        fail(f"docs/11-vmid-plan.md contains duplicate VMID entry: {vmid}")
    return set(values)


def load_mapping(path: Path) -> dict | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        fail(f"cannot parse {path.relative_to(ROOT)}: {exc}")
        return None
    if not isinstance(data, dict):
        fail(f"{path.relative_to(ROOT)} must contain a YAML mapping")
        return None
    return data


def load_schema(path: Path) -> Draft202012Validator:
    try:
        schema = yaml.safe_load(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        fail(f"invalid schema {path.relative_to(ROOT)}: {exc}")
        return Draft202012Validator({})
    return Draft202012Validator(schema)


def format_schema_path(error) -> str:
    parts = [str(part) for part in error.absolute_path]
    return ".".join(parts) if parts else "<root>"


def validate_schema(
    *,
    rel: Path,
    data: dict,
    validator: Draft202012Validator,
    label: str = "schema",
) -> bool:
    schema_errors = sorted(
        validator.iter_errors(data),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    for error in schema_errors:
        fail(
            f"{rel}: {label} error at {format_schema_path(error)}: "
            f"{error.message}"
        )
    return not schema_errors


def deep_merge(base: dict, overlay: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def walk_mapping_keys(value, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = path + (str(key),)
            yield child_path, str(key)
            yield from walk_mapping_keys(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_mapping_keys(child, path + (str(index),))


def walk_leaf_values(value, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk_leaf_values(child, path + (str(key),))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_leaf_values(child, path + (str(index),))
    else:
        yield path, value


def nested_get(mapping: dict, path: tuple[str, ...]):
    value = mapping
    for part in path:
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def path_text(path: tuple[str, ...]) -> str:
    return ".".join(path)


def validate_manifest_secrets(rel: Path, data: dict) -> None:
    for key_path, key in walk_mapping_keys(data):
        if key.lower() in FORBIDDEN_MANIFEST_KEYS:
            fail(
                f"{rel}: secret-like key is forbidden: "
                f"{'.'.join(key_path)}"
            )


def validate_network_defaults(defaults: dict) -> dict[str, dict]:
    rel = GUEST_DEFAULTS.relative_to(ROOT)
    stages = defaults.get("network_stages", {})
    validated: dict[str, dict] = {}

    for stage in ("current", "target"):
        stage_data = stages.get(stage)
        if not isinstance(stage_data, dict):
            continue

        subnet_text = stage_data.get("subnet")
        gateway_text = stage_data.get("gateway")
        try:
            subnet = ipaddress.ip_network(subnet_text, strict=True)
        except ValueError as exc:
            fail(f"{rel}: invalid network_stages.{stage}.subnet {subnet_text!r}: {exc}")
            continue

        if not isinstance(subnet, ipaddress.IPv4Network):
            fail(f"{rel}: network_stages.{stage}.subnet must be IPv4")
            continue
        if subnet.prefixlen != 16:
            fail(f"{rel}: network_stages.{stage}.subnet must use /16")

        try:
            gateway = ipaddress.ip_address(gateway_text)
        except ValueError as exc:
            fail(
                f"{rel}: invalid network_stages.{stage}.gateway "
                f"{gateway_text!r}: {exc}"
            )
            continue

        if not isinstance(gateway, ipaddress.IPv4Address):
            fail(f"{rel}: network_stages.{stage}.gateway must be IPv4")
            continue
        if gateway not in subnet:
            fail(
                f"{rel}: network_stages.{stage}.gateway {gateway} "
                f"is outside {subnet}"
            )
            continue
        if gateway in {subnet.network_address, subnet.broadcast_address}:
            fail(
                f"{rel}: network_stages.{stage}.gateway {gateway} "
                f"cannot be network/broadcast address"
            )
            continue

        validated[stage] = {"subnet": subnet, "gateway": gateway}

    return validated


def resolve_effective_manifest(
    *,
    rel: Path,
    source: dict,
    defaults: dict,
) -> dict | None:
    profile_name = source.get("profile")
    profiles = defaults.get("profiles", {})
    profile = profiles.get(profile_name)

    if not isinstance(profile, dict):
        fail(f"{rel}: unknown guest profile {profile_name!r}")
        return None

    effective = deep_merge(defaults.get("defaults", {}), profile)
    effective = deep_merge(effective, source)

    network = effective.get("network")
    if isinstance(network, dict):
        stage_defaults = defaults.get("network_stages", {})
        for stage in ("current", "target"):
            stage_data = network.get(stage)
            if not isinstance(stage_data, dict):
                continue
            ipv4 = stage_data.get("ipv4")
            central = stage_defaults.get(stage)
            if isinstance(ipv4, dict) and isinstance(central, dict):
                ipv4["gateway"] = central.get("gateway")

    return effective


def expected_management_ip(
    vmid: int,
    stage_config: dict,
) -> ipaddress.IPv4Address:
    subnet: ipaddress.IPv4Network = stage_config["subnet"]
    octets = str(subnet.network_address).split(".")
    vmid_text = f"{vmid:03d}"
    third = int(vmid_text[0])
    fourth = int(vmid_text[1:])
    return ipaddress.ip_address(f"{octets[0]}.{octets[1]}.{third}.{fourth}")


def validate_source_overrides(
    *,
    rel: Path,
    source: dict,
    defaults: dict,
) -> None:
    if not source.get("deployable"):
        return

    profile_name = source.get("profile")
    profile = defaults.get("profiles", {}).get(profile_name)
    if not isinstance(profile, dict):
        return

    inherited = deep_merge(defaults.get("defaults", {}), profile)

    for path, value in walk_leaf_values(source):
        inherited_value = nested_get(inherited, path)
        if inherited_value is _MISSING:
            continue

        dotted = path_text(path)
        if value == inherited_value:
            warn(
                f"{rel}: избыточное переопределение {dotted}={value!r}; "
                "такое же значение уже наследуется из defaults/profile"
            )
            continue

        if path in NETWORK_MIGRATION_PATHS:
            warn(
                f"{rel}: {dotted} переопределяет общий сетевой default "
                f"{inherited_value!r} на {value!r}; проверьте, что это "
                "осознанный этап сетевой миграции"
            )
        elif path in PROJECT_DEFAULT_OVERRIDE_PATHS:
            warn(
                f"{rel}: {dotted} переопределяет проектное значение "
                f"{inherited_value!r} на {value!r}"
            )


def validate_stage_address(
    *,
    rel: Path,
    vmid: int,
    stage: str,
    network: dict,
    stage_configs: dict[str, dict],
    static_ips: dict[ipaddress.IPv4Address, Path],
    require_gateway: bool,
) -> ipaddress.IPv4Address | None:
    stage_data = network.get(stage)
    if stage_data is None:
        return None

    ipv4 = stage_data.get("ipv4", {})
    address_text = ipv4.get("address")

    try:
        interface = ipaddress.ip_interface(address_text)
    except ValueError as exc:
        fail(f"{rel}: invalid network.{stage}.ipv4.address {address_text!r}: {exc}")
        return None

    if not isinstance(interface, ipaddress.IPv4Interface):
        fail(f"{rel}: network.{stage}.ipv4.address must be IPv4")
        return None

    config = stage_configs.get(stage)
    if config is None:
        fail(f"{rel}: no validated central network stage {stage!r}")
        return None

    subnet: ipaddress.IPv4Network = config["subnet"]
    if interface.network.prefixlen != subnet.prefixlen:
        fail(
            f"{rel}: network.{stage}.ipv4.address must use /{subnet.prefixlen}, "
            f"got /{interface.network.prefixlen}"
        )

    address = interface.ip
    if address not in subnet:
        fail(f"{rel}: network.{stage}.ipv4.address {address} is outside {subnet}")

    if address in {subnet.network_address, subnet.broadcast_address}:
        fail(
            f"{rel}: network.{stage}.ipv4.address {address} "
            f"cannot be network/broadcast address of {subnet}"
        )

    expected_gateway: ipaddress.IPv4Address = config["gateway"]
    if address == expected_gateway:
        fail(
            f"{rel}: network.{stage}.ipv4.address {address} "
            "cannot equal the default gateway"
        )

    if address in static_ips:
        fail(f"duplicate static IP {address}: {static_ips[address]} and {rel}")
    else:
        static_ips[address] = rel

    expected = expected_management_ip(vmid, config)
    if address != expected:
        warn(
            f"{rel}: management IP стадии {stage} ({address}) не соответствует "
            f"правилу VMID {vmid}; ожидается {expected}"
        )

    if not require_gateway:
        return address

    gateway_text = ipv4.get("gateway")
    try:
        gateway = ipaddress.ip_address(gateway_text)
    except ValueError as exc:
        fail(f"{rel}: invalid effective {stage} gateway {gateway_text!r}: {exc}")
        return address

    if gateway != expected_gateway:
        fail(
            f"{rel}: effective {stage} gateway must come from guests/defaults.yaml "
            f"and equal {expected_gateway}, got {gateway}"
        )

    return address


def validate_network_pair(
    *,
    rel: Path,
    addresses: dict[str, ipaddress.IPv4Address],
    stage_configs: dict[str, dict],
) -> None:
    current = addresses.get("current")
    target = addresses.get("target")
    if current is None or target is None:
        return

    if current == target:
        fail(f"{rel}: current and target management IP cannot be identical ({current})")
        return

    current_cfg = stage_configs.get("current")
    target_cfg = stage_configs.get("target")
    if current_cfg is None or target_cfg is None:
        return

    current_offset = int(current) - int(current_cfg["subnet"].network_address)
    target_offset = int(target) - int(target_cfg["subnet"].network_address)
    if current_offset != target_offset:
        warn(
            f"{rel}: current IP {current} и target IP {target} имеют разные "
            "идентификаторные части; проверьте соответствие адресов между стадиями"
        )


def validate_resource_semantics(rel: Path, data: dict) -> None:
    resources = data.get("resources")
    if not isinstance(resources, dict):
        return

    memory = resources.get("memory_mb")
    ballooning = resources.get("ballooning_mb")
    if (
        isinstance(memory, int)
        and isinstance(ballooning, int)
        and ballooning > memory
    ):
        fail(
            f"{rel}: resources.ballooning_mb ({ballooning}) "
            f"cannot exceed resources.memory_mb ({memory})"
        )


def validate_state_warnings(rel: Path, source: dict, effective: dict) -> None:
    state = source.get("state")
    deployable = source.get("deployable")

    if deployable and state in {"bootstrap", "legacy"}:
        warn(
            f"{rel}: state={state!r} используется вместе с deployable: true; "
            "проверьте, что временный/устаревший объект действительно должен "
            "разворачиваться универсальным deployer"
        )

    description = source.get("description", "")
    if deployable and isinstance(description, str):
        lowered = description.lower()
        found = next(
            (marker for marker in UNRESOLVED_DESCRIPTION_MARKERS if marker in lowered),
            None,
        )
        if found is not None:
            warn(
                f"{rel}: deployable-гость содержит в description маркер "
                f"незавершённости {found!r}"
            )

    boot = effective.get("boot")
    if state == "active" and isinstance(boot, dict) and boot.get("onboot") is False:
        warn(
            f"{rel}: активный объект имеет boot.onboot=false; после перезагрузки "
            "PVE он не запустится автоматически"
        )

    if deployable:
        placement = effective.get("placement")
        pool = placement.get("pool") if isinstance(placement, dict) else None
        if effective.get("protection") is True and pool == "managed":
            warn(
                f"{rel}: protection=true у гостя в pool 'managed'; автоматический "
                "reconcile/delete может быть ограничен защитой"
            )


def validate_deployable(rel: Path, source: dict, effective: dict) -> None:
    if not source["deployable"]:
        return

    for forbidden in ("type", "vm", "lxc"):
        if forbidden in source:
            fail(
                f"{rel}: deployable guest must get {forbidden!r} from its profile; "
                f"create or change a profile instead of overriding it locally"
            )

    vmid = effective["vmid"]
    guest_type = effective["type"]
    pool = effective["placement"]["pool"]

    if vmid in SELF_MANAGED_EXCLUSIONS:
        if pool is not None:
            fail(f"{rel}: VMID {vmid} must not be placed in ordinary managed pool")
    elif pool != "managed":
        fail(f"{rel}: deployable managed guest must use placement.pool: managed")

    ssh = effective["management"]["ssh"]
    if ssh["user"] != "ops":
        fail(f"{rel}: effective management.ssh.user must be 'ops'")
    if ssh["port"] != 22:
        fail(f"{rel}: effective management.ssh.port must be 22")

    if guest_type == "vm":
        source_vm = effective["vm"]["source"]
        if source_vm["template_vmid"] == vmid:
            fail(f"{rel}: VM cannot clone from itself")
        if effective["vm"]["guest_agent"] is not True:
            fail(f"{rel}: deployable VM must enable QEMU guest agent")

    if guest_type == "lxc":
        source_lxc = effective["lxc"]["source"]
        ostemplate = source_lxc["ostemplate"]
        lowered = ostemplate.lower()
        if any(marker in lowered for marker in FORBIDDEN_SOURCE_MARKERS):
            fail(f"{rel}: lxc.source.ostemplate must be pinned, got {ostemplate!r}")
        if not LXC_OSTEMPLATE_RE.fullmatch(ostemplate):
            fail(
                f"{rel}: lxc.source.ostemplate must be a concrete Proxmox "
                f"vztmpl volume, got {ostemplate!r}"
            )


def validate_profiles(defaults: dict) -> None:
    rel = GUEST_DEFAULTS.relative_to(ROOT)
    for name, profile in defaults.get("profiles", {}).items():
        if not isinstance(profile, dict):
            continue

        guest_type = profile.get("type")
        if guest_type == "vm":
            vm = profile.get("vm", {})
            if vm.get("guest_agent") is not True:
                fail(f"{rel}: profile {name!r} must enable QEMU guest agent")

        if guest_type == "lxc":
            lxc = profile.get("lxc", {})
            source = lxc.get("source", {})
            ostemplate = source.get("ostemplate", "")
            lowered = ostemplate.lower()
            if any(marker in lowered for marker in FORBIDDEN_SOURCE_MARKERS):
                fail(
                    f"{rel}: profile {name!r} lxc.source.ostemplate "
                    f"must be pinned, got {ostemplate!r}"
                )
            if not LXC_OSTEMPLATE_RE.fullmatch(ostemplate):
                fail(
                    f"{rel}: profile {name!r} lxc.source.ostemplate must be "
                    f"a concrete Proxmox vztmpl volume, got {ostemplate!r}"
                )
            if lxc.get("container_runtime") == "docker":
                features = lxc.get("features", {})
                if lxc.get("unprivileged") is not True:
                    fail(f"{rel}: Docker profile {name!r} must be unprivileged")
                if features.get("nesting") is not True or features.get("keyctl") is not True:
                    fail(
                        f"{rel}: Docker profile {name!r} must enable nesting and keyctl"
                    )


def validate_guest_manifests() -> None:
    plan = planned_vmids()
    source_validator = load_schema(GUEST_SCHEMA)
    defaults_validator = load_schema(GUEST_DEFAULTS_SCHEMA)
    effective_validator = load_schema(GUEST_EFFECTIVE_SCHEMA)

    defaults = load_mapping(GUEST_DEFAULTS)
    if defaults is None:
        return

    defaults_rel = GUEST_DEFAULTS.relative_to(ROOT)
    validate_manifest_secrets(defaults_rel, defaults)
    defaults_ok = validate_schema(
        rel=defaults_rel,
        data=defaults,
        validator=defaults_validator,
        label="defaults schema",
    )
    stage_configs = validate_network_defaults(defaults)
    validate_profiles(defaults)
    if not defaults_ok:
        return

    seen_vmids: dict[int, Path] = {}
    manifest_vmids: set[int] = set()
    static_ips: dict[ipaddress.IPv4Address, Path] = {}
    used_profiles: set[str] = set()
    profiles = defaults.get("profiles", {})

    for directory in sorted(p for p in GUESTS.iterdir() if p.is_dir()):
        match = GUEST_DIR_RE.match(directory.name)
        if not match:
            continue

        expected_vmid = int(match.group(1))
        expected_name = match.group(2)
        manifest = directory / "guest.yaml"
        rel = manifest.relative_to(ROOT)

        if not (directory / "README.md").exists():
            warn(
                f"{directory.relative_to(ROOT)}: отсутствует README.md "
                "с документацией гостя"
            )

        if not manifest.exists():
            fail(
                f"{directory.relative_to(ROOT)}: missing guest.yaml; "
                "reserved/undecided guests must use deployable: false"
            )
            continue

        manifest_vmids.add(expected_vmid)
        data = load_mapping(manifest)
        if data is None:
            continue

        if not validate_schema(
            rel=rel,
            data=data,
            validator=source_validator,
            label="source schema",
        ):
            continue

        vmid = data["vmid"]

        if vmid != expected_vmid:
            fail(f"{rel}: vmid {vmid!r} does not match directory VMID {expected_vmid}")
        if data["name"] != expected_name:
            fail(
                f"{rel}: name {data['name']!r} does not match "
                f"directory name {expected_name!r}"
            )

        if vmid in seen_vmids:
            fail(f"duplicate guest VMID {vmid}: {seen_vmids[vmid]} and {rel}")
        else:
            seen_vmids[vmid] = rel

        if vmid not in plan and data["state"] != "legacy":
            fail(f"{rel}: VMID {vmid} is not listed in docs/11-vmid-plan.md")

        validate_manifest_secrets(rel, data)

        if data["deployable"]:
            profile_name = data.get("profile")
            if profile_name in profiles:
                used_profiles.add(profile_name)

            validate_source_overrides(rel=rel, source=data, defaults=defaults)
            effective = resolve_effective_manifest(rel=rel, source=data, defaults=defaults)
            if effective is None:
                continue

            if not validate_schema(
                rel=rel,
                data=effective,
                validator=effective_validator,
                label="effective schema",
            ):
                continue

            validate_resource_semantics(rel, effective)
            network = effective["network"]
            addresses: dict[str, ipaddress.IPv4Address] = {}
            for stage in ("current", "target"):
                address = validate_stage_address(
                    rel=rel,
                    vmid=vmid,
                    stage=stage,
                    network=network,
                    stage_configs=stage_configs,
                    static_ips=static_ips,
                    require_gateway=True,
                )
                if address is not None:
                    addresses[stage] = address

            validate_network_pair(
                rel=rel,
                addresses=addresses,
                stage_configs=stage_configs,
            )
            validate_deployable(rel, data, effective)
            validate_state_warnings(rel, data, effective)
        else:
            validate_resource_semantics(rel, data)
            network = data.get("network")
            addresses: dict[str, ipaddress.IPv4Address] = {}
            if isinstance(network, dict):
                for stage in ("current", "target"):
                    address = validate_stage_address(
                        rel=rel,
                        vmid=vmid,
                        stage=stage,
                        network=network,
                        stage_configs=stage_configs,
                        static_ips=static_ips,
                        require_gateway=False,
                    )
                    if address is not None:
                        addresses[stage] = address

                validate_network_pair(
                    rel=rel,
                    addresses=addresses,
                    stage_configs=stage_configs,
                )

            validate_state_warnings(rel, data, data)

    for vmid in sorted(plan - manifest_vmids):
        warn(
            f"{VMID_PLAN.relative_to(ROOT)}: VMID {vmid} присутствует в плане, "
            "но для него нет каталога гостя с guest.yaml"
        )

    for profile_name in sorted(set(profiles) - used_profiles):
        warn(
            f"{defaults_rel}: профиль {profile_name!r} не используется "
            "ни одним deployable-гостем"
        )


def validate_secret_files(files: list[Path]) -> None:
    for path in files:
        rel = path.relative_to(ROOT)
        name = path.name.lower()

        forbidden_name = (
            (name.startswith(".env") and name not in {".env.example", ".env.sample"})
            or name in {"id_rsa", "id_ed25519", "privatekey", "presharedkey"}
            or path.suffix.lower() in {".p12", ".pfx", ".private", ".psk"}
        )
        if forbidden_name:
            fail(f"possible secret file is tracked: {rel}")
            continue

        try:
            if path.stat().st_size > 2_000_000:
                continue
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        if any(marker in text for marker in PRIVATE_KEY_MARKERS):
            fail(f"private key material detected in tracked file: {rel}")


def validate_markdown_links(files: list[Path]) -> None:
    for path in files:
        if path.suffix.lower() != ".md":
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            fail(f"cannot read {path.relative_to(ROOT)}: {exc}")
            continue

        for raw_target in MARKDOWN_LINK_RE.findall(text):
            target = raw_target.strip().split()[0].strip("<>\"'")
            if not target or target.startswith(("http://", "https://", "mailto:", "#")):
                continue

            target = unquote(target.split("#", 1)[0].split("?", 1)[0])
            if not target:
                continue

            resolved = (
                (ROOT / target.lstrip("/"))
                if target.startswith("/")
                else (path.parent / target)
            )
            if not resolved.exists():
                shown = resolved.relative_to(ROOT) if resolved.is_relative_to(ROOT) else resolved
                fail(
                    f"broken local link in {path.relative_to(ROOT)}: "
                    f"{raw_target} -> {shown}"
                )


def main() -> int:
    files = tracked_files()
    validate_yaml(files)
    validate_guest_manifests()
    validate_secret_files(files)
    validate_markdown_links(files)

    unique_warnings = list(dict.fromkeys(warnings))
    if unique_warnings:
        print("Предупреждения проверки репозитория:")
        for warning in unique_warnings:
            print(f" - {warning}")

    if errors:
        print("Repository validation failed:")
        for error in errors:
            print(f" - {error}")
        return 1

    print("Repository validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

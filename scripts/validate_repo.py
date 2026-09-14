#!/usr/bin/env python3
"""Consistency and deploy-readiness checks for the Proxmox infrastructure repository."""

from __future__ import annotations

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
VMID_PLAN = ROOT / "docs" / "11-vmid-plan.md"
GUEST_SCHEMA = ROOT / "schemas" / "guest.schema.yaml"

GUEST_DIR_RE = re.compile(r"^(\d{3})-(.+)$")
PLAN_VMID_RE = re.compile(r"^- `(?P<vmid>\d{3})`\s+—", re.MULTILINE)
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
LXC_OSTEMPLATE_RE = re.compile(
    r"^[A-Za-z0-9._-]+:vztmpl/[A-Za-z0-9._+-]+_amd64\.tar\.(?:zst|gz|xz)$"
)
FORBIDDEN_SOURCE_MARKERS = ("<", ">", "*", "?", "13.x", "latest", "tbd", "todo")
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
PRIVATE_KEY_MARKERS = tuple(
    "-----BEGIN " + key_type + "-----"
    for key_type in (
        "PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
    )
)

errors: list[str] = []


def fail(message: str) -> None:
    errors.append(message)


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


def load_guest_schema() -> Draft202012Validator:
    try:
        schema = yaml.safe_load(GUEST_SCHEMA.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        fail(f"invalid guest schema {GUEST_SCHEMA.relative_to(ROOT)}: {exc}")
        return Draft202012Validator({})
    return Draft202012Validator(schema)


def format_schema_path(error) -> str:
    parts = [str(part) for part in error.absolute_path]
    return ".".join(parts) if parts else "<root>"


def validate_manifest_schema(
    *, rel: Path, data: dict, schema_validator: Draft202012Validator
) -> bool:
    manifest_errors = sorted(
        schema_validator.iter_errors(data),
        key=lambda error: (list(error.absolute_path), error.message),
    )
    for error in manifest_errors:
        fail(f"{rel}: schema error at {format_schema_path(error)}: {error.message}")
    return not manifest_errors


def expected_management_ip(vmid: int, stage: str) -> ipaddress.IPv4Address:
    text = f"{vmid:03d}"
    third = int(text[0])
    fourth = int(text[1:])
    if stage == "current":
        return ipaddress.ip_address(f"192.168.{third}.{fourth}")
    if stage == "target":
        return ipaddress.ip_address(f"10.0.{third}.{fourth}")
    raise ValueError(f"unknown network stage: {stage}")


def validate_stage_ip(
    *,
    rel: Path,
    vmid: int,
    stage: str,
    network: dict,
    static_ips: dict[ipaddress.IPv4Address, Path],
) -> None:
    stage_data = network.get(stage)
    if stage_data is None:
        return

    ipv4 = stage_data.get("ipv4", {})
    address_text = ipv4.get("address")
    gateway_text = ipv4.get("gateway")

    try:
        interface = ipaddress.ip_interface(address_text)
    except ValueError as exc:
        fail(f"{rel}: invalid network.{stage}.ipv4.address {address_text!r}: {exc}")
        return

    if not isinstance(interface, ipaddress.IPv4Interface):
        fail(f"{rel}: network.{stage}.ipv4.address must be IPv4")
        return

    if interface.network.prefixlen != 16:
        fail(
            f"{rel}: network.{stage}.ipv4.address must use /16, "
            f"got /{interface.network.prefixlen}"
        )

    address = interface.ip
    if address in static_ips:
        fail(f"duplicate static IP {address}: {static_ips[address]} and {rel}")
    else:
        static_ips[address] = rel

    expected = expected_management_ip(vmid, stage)
    if address != expected:
        fail(
            f"{rel}: {stage} management IP {address} does not match VMID rule, "
            f"expected {expected}"
        )

    try:
        gateway = ipaddress.ip_address(gateway_text)
    except ValueError as exc:
        fail(f"{rel}: invalid network.{stage}.ipv4.gateway {gateway_text!r}: {exc}")
        return

    if not isinstance(gateway, ipaddress.IPv4Address):
        fail(f"{rel}: network.{stage}.ipv4.gateway must be IPv4")
        return

    if gateway not in interface.network:
        fail(
            f"{rel}: network.{stage}.ipv4.gateway {gateway} is outside "
            f"{interface.network}"
        )

    expected_gateway = ipaddress.ip_address(
        "192.168.1.1" if stage == "current" else "10.0.0.1"
    )
    if gateway != expected_gateway:
        fail(f"{rel}: {stage} gateway must be {expected_gateway}, got {gateway}")


def walk_mapping_keys(value, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = path + (str(key),)
            yield child_path, str(key)
            yield from walk_mapping_keys(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from walk_mapping_keys(child, path + (str(index),))


def validate_manifest_secrets(rel: Path, data: dict) -> None:
    for key_path, key in walk_mapping_keys(data):
        if key.lower() in FORBIDDEN_MANIFEST_KEYS:
            fail(
                f"{rel}: secret-like key is forbidden in guest.yaml: "
                f"{'.'.join(key_path)}"
            )


def validate_deployable(rel: Path, data: dict) -> None:
    if not data["deployable"]:
        return

    vmid = data["vmid"]
    guest_type = data["type"]
    pool = data["placement"]["pool"]

    if guest_type == "undecided":
        fail(f"{rel}: deployable guest cannot have type 'undecided'")

    if vmid in SELF_MANAGED_EXCLUSIONS:
        if pool is not None:
            fail(f"{rel}: VMID {vmid} must not be placed in ordinary managed pool")
    elif pool != "managed":
        fail(f"{rel}: deployable managed guest must use placement.pool: managed")

    ssh = data["management"]["ssh"]
    if ssh["user"] != "ops":
        fail(f"{rel}: deployable guest management.ssh.user must be 'ops'")
    if ssh["port"] != 22:
        fail(f"{rel}: deployable guest management.ssh.port must be 22")

    if guest_type == "vm":
        source = data["vm"]["source"]
        if source["template_vmid"] == vmid:
            fail(f"{rel}: VM cannot clone from itself")
        if data["vm"]["guest_agent"] is not True:
            fail(f"{rel}: deployable VM must enable QEMU guest agent")

    if guest_type == "lxc":
        source = data["lxc"]["source"]
        ostemplate = source["ostemplate"]
        lowered = ostemplate.lower()
        if any(marker in lowered for marker in FORBIDDEN_SOURCE_MARKERS):
            fail(f"{rel}: lxc.source.ostemplate must be pinned, got {ostemplate!r}")
        if not LXC_OSTEMPLATE_RE.fullmatch(ostemplate):
            fail(
                f"{rel}: lxc.source.ostemplate must be a concrete Proxmox "
                f"vztmpl volume, got {ostemplate!r}"
            )


def validate_guest_manifests() -> None:
    plan = planned_vmids()
    schema_validator = load_guest_schema()
    seen_vmids: dict[int, Path] = {}
    static_ips: dict[ipaddress.IPv4Address, Path] = {}

    for directory in sorted(p for p in GUESTS.iterdir() if p.is_dir()):
        match = GUEST_DIR_RE.match(directory.name)
        if not match:
            continue

        expected_vmid = int(match.group(1))
        expected_name = match.group(2)
        manifest = directory / "guest.yaml"
        rel = manifest.relative_to(ROOT)

        if not manifest.exists():
            fail(
                f"{directory.relative_to(ROOT)}: missing guest.yaml; "
                "reserved/undecided guests must use deployable: false"
            )
            continue

        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except Exception as exc:
            fail(f"cannot parse {rel}: {exc}")
            continue

        if not isinstance(data, dict):
            fail(f"{rel} must contain a YAML mapping")
            continue

        if not validate_manifest_schema(
            rel=rel, data=data, schema_validator=schema_validator
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

        network = data.get("network")
        if isinstance(network, dict):
            for stage in ("current", "target"):
                validate_stage_ip(
                    rel=rel,
                    vmid=vmid,
                    stage=stage,
                    network=network,
                    static_ips=static_ips,
                )

        validate_deployable(rel, data)


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
                shown = (
                    resolved.relative_to(ROOT)
                    if resolved.is_relative_to(ROOT)
                    else resolved
                )
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

    if errors:
        print("Repository validation failed:")
        for error in errors:
            print(f" - {error}")
        return 1

    print("Repository validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

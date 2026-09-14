#!/usr/bin/env python3
"""Consistency checks for the Proxmox infrastructure repository."""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import yaml

ROOT = Path(__file__).resolve().parents[1]
GUESTS = ROOT / "guests"
VMID_PLAN = ROOT / "docs" / "vmid-plan.md"
ALLOWED_TYPES = {"vm", "lxc"}
ALLOWED_STATES = {"planned", "active", "bootstrap", "legacy"}
GUEST_DIR_RE = re.compile(r"^(\d{3})-(.+)$")
PLAN_VMID_RE = re.compile(r"^- `(?P<vmid>\d{3})`\s+—", re.MULTILINE)
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
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
        fail(f"docs/vmid-plan.md contains duplicate VMID entry: {vmid}")
    return set(values)


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
    static_ips: dict[ipaddress._BaseAddress, Path],
) -> None:
    stage_data = network.get(stage) or {}
    ipv4 = stage_data.get("ipv4") or {} if isinstance(stage_data, dict) else {}
    address_text = ipv4.get("address") if isinstance(ipv4, dict) else None
    gateway_text = ipv4.get("gateway") if isinstance(ipv4, dict) else None

    if not address_text:
        fail(f"{rel}: planned guest must define network.{stage}.ipv4.address")
        return

    try:
        interface = ipaddress.ip_interface(address_text)
    except ValueError as exc:
        fail(f"{rel}: invalid network.{stage}.ipv4.address {address_text!r}: {exc}")
        return

    address = interface.ip
    if address in static_ips:
        fail(f"duplicate static IP {address}: {static_ips[address]} and {rel}")
    else:
        static_ips[address] = rel

    expected = expected_management_ip(vmid, stage)
    if address != expected:
        fail(f"{rel}: {stage} management IP {address} does not match VMID rule, expected {expected}")

    expected_gateway = "192.168.1.1" if stage == "current" else "10.0.0.1"
    if gateway_text != expected_gateway:
        fail(
            f"{rel}: {stage} gateway must be {expected_gateway!r}, "
            f"got {gateway_text!r}"
        )


def validate_guest_manifests() -> None:
    plan = planned_vmids()
    seen_vmids: dict[int, Path] = {}
    static_ips: dict[ipaddress._BaseAddress, Path] = {}

    for directory in sorted(p for p in GUESTS.iterdir() if p.is_dir()):
        match = GUEST_DIR_RE.match(directory.name)
        if not match:
            continue

        expected_vmid = int(match.group(1))
        expected_name = match.group(2)
        manifest = directory / "guest.yaml"

        if not manifest.exists():
            print(f"SKIP {directory.relative_to(ROOT)}: no guest.yaml")
            continue

        try:
            data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        except Exception as exc:
            fail(f"cannot parse {manifest.relative_to(ROOT)}: {exc}")
            continue

        if not isinstance(data, dict):
            fail(f"{manifest.relative_to(ROOT)} must contain a YAML mapping")
            continue

        required = {"schema_version", "vmid", "name", "type", "state"}
        missing = sorted(required - set(data))
        if missing:
            fail(f"{manifest.relative_to(ROOT)} missing required fields: {', '.join(missing)}")
            continue

        vmid = data["vmid"]
        rel = manifest.relative_to(ROOT)

        if data["schema_version"] != 1:
            fail(f"{rel}: schema_version must be 1")
        if vmid != expected_vmid:
            fail(f"{rel}: vmid {vmid!r} does not match directory VMID {expected_vmid}")
        if data["name"] != expected_name:
            fail(f"{rel}: name {data['name']!r} does not match directory name {expected_name!r}")
        if data["type"] not in ALLOWED_TYPES:
            fail(f"{rel}: type must be one of {sorted(ALLOWED_TYPES)}, got {data['type']!r}")
        if data["state"] not in ALLOWED_STATES:
            fail(f"{rel}: state must be one of {sorted(ALLOWED_STATES)}, got {data['state']!r}")
        if "protection" in data and not isinstance(data["protection"], bool):
            fail(f"{rel}: protection must be a boolean when specified")

        if isinstance(vmid, int):
            if vmid in seen_vmids:
                fail(f"duplicate guest VMID {vmid}: {seen_vmids[vmid]} and {rel}")
            else:
                seen_vmids[vmid] = rel

            if vmid not in plan and data["state"] not in {"bootstrap", "legacy"}:
                fail(f"{rel}: VMID {vmid} is not listed in docs/vmid-plan.md")

        management = data.get("management") or {}
        ssh = management.get("ssh") or {} if isinstance(management, dict) else {}
        ssh_user = ssh.get("user") if isinstance(ssh, dict) else None

        if ssh_user and ssh_user != "ops":
            fail(f"{rel}: management.ssh.user must be 'ops', got {ssh_user!r}")

        if data["type"] == "vm" and data["state"] in {"planned", "bootstrap"}:
            if not ssh_user:
                fail(f"{rel}: managed planned/bootstrap VM must define management.ssh.user")

        if data["state"] == "planned" and isinstance(vmid, int):
            network = data.get("network") or {}
            if not isinstance(network, dict):
                fail(f"{rel}: network must be a mapping")
                continue
            for stage in ("current", "target"):
                validate_stage_ip(
                    rel=rel,
                    vmid=vmid,
                    stage=stage,
                    network=network,
                    static_ips=static_ips,
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

            resolved = (ROOT / target.lstrip("/")) if target.startswith("/") else (path.parent / target)
            if not resolved.exists():
                shown = resolved.relative_to(ROOT) if resolved.is_relative_to(ROOT) else resolved
                fail(f"broken local link in {path.relative_to(ROOT)}: {raw_target} -> {shown}")


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

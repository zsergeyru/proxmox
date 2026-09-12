#!/usr/bin/env python3
"""Lightweight consistency checks for the Proxmox infrastructure repository."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

import yaml

ROOT = Path(__file__).resolve().parents[1]
GUESTS = ROOT / "guests"
ALLOWED_TYPES = {"vm", "lxc"}
ALLOWED_STATES = {"planned", "active", "bootstrap", "legacy"}
GUEST_DIR_RE = re.compile(r"^(\d{3})-(.+)$")
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
PRIVATE_KEY_MARKERS = (
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
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
        except Exception as exc:  # PyYAML exposes several parser exception classes.
            fail(f"invalid YAML: {path.relative_to(ROOT)}: {exc}")


def validate_guest_manifests() -> None:
    for directory in sorted(p for p in GUESTS.iterdir() if p.is_dir()):
        match = GUEST_DIR_RE.match(directory.name)
        if not match:
            continue

        expected_vmid = int(match.group(1))
        expected_name = match.group(2)
        manifest = directory / "guest.yaml"

        # Some guests intentionally have no manifest until key design choices are made
        # (for example 501-frigate before VM/LXC is selected).
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

        if data["schema_version"] != 1:
            fail(f"{manifest.relative_to(ROOT)}: schema_version must be 1")
        if data["vmid"] != expected_vmid:
            fail(
                f"{manifest.relative_to(ROOT)}: vmid {data['vmid']!r} does not match "
                f"directory VMID {expected_vmid}"
            )
        if data["name"] != expected_name:
            fail(
                f"{manifest.relative_to(ROOT)}: name {data['name']!r} does not match "
                f"directory name {expected_name!r}"
            )
        if data["type"] not in ALLOWED_TYPES:
            fail(
                f"{manifest.relative_to(ROOT)}: type must be one of "
                f"{sorted(ALLOWED_TYPES)}, got {data['type']!r}"
            )
        if data["state"] not in ALLOWED_STATES:
            fail(
                f"{manifest.relative_to(ROOT)}: state must be one of "
                f"{sorted(ALLOWED_STATES)}, got {data['state']!r}"
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
                fail(
                    f"broken local link in {path.relative_to(ROOT)}: {raw_target} "
                    f"-> {resolved.relative_to(ROOT) if resolved.is_relative_to(ROOT) else resolved}"
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

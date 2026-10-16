"""Каталог приложений для Homarr из Git без обращения к работающим гостям."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .guest_catalog import deployable_guests, guest_management_address
from .portal import _load_provision, portal_definitions


def _images(value: Any, prefix: str = "") -> list[dict[str, str | None]]:
    """Найти закреплённые образы, не выдавая их за установленные."""
    result: list[dict[str, str | None]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            source = f"{prefix}.{key}" if prefix else str(key)
            if key == "image" and isinstance(child, str) and child.strip():
                result.append({
                    "source": prefix,
                    "configured_image": child.strip(),
                    "installed_version": None,
                    "available_version": None,
                })
            elif key not in {"portal", "persistence"}:
                result.extend(_images(child, source))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.extend(_images(child, f"{prefix}[{index}]"))
    return result


def build_catalog(repo_root: Path) -> dict[str, Any]:
    """Составить машинный каталог; сведения о PVE здесь недоступны.

    Не устанавливать приложения, не обращаться к сети и не выполнять команд.
    """
    guests: list[dict[str, Any]] = []
    for identity in deployable_guests(repo_root):
        provision = _load_provision(identity)
        images = _images(provision)
        archive = provision.get("photo_archive")
        if isinstance(archive, dict) and isinstance(archive.get("immich_version"), str):
            images.append({
                "source": "photo_archive.immich_version",
                "configured_image": f"immich:{archive['immich_version']}",
                "installed_version": None,
                "available_version": None,
            })
        services = [
            {"name": service["name"], "url": service["url"]}
            for service in portal_definitions(repo_root, identity)
            if set(service["targets"]) & {"local", "home"}
        ]
        guests.append({
            "vmid": identity.vmid,
            "name": identity.name,
            "role": identity.role,
            "address": guest_management_address(repo_root, identity),
            "managed": True,
            "state": "unknown",
            "kind": None,
            "services": services,
            "versions": images,
            "actions": [],
        })
    return {
        "schema_version": 1,
        "source": "git",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pve_available": False,
        "actions_enabled": False,
        "guests": guests,
    }

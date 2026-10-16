"""Единый каталог гостей для панели Homarr без копирования VMID в 140."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import InfraManagerError
from .guest_catalog import deployable_guests, guest_management_address
from .guest_operations import GUEST_OPERATIONS, _expected_resource_type, operation_guests
from .portal import _load_provision, portal_definitions
from .pve import PveClient


def _images(value: Any, prefix: str = "") -> list[dict[str, str]]:
    """Найти только явно объявленные образы; не считать их установленными."""
    result: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_path = f"{prefix}.{key}" if prefix else str(key)
            if key == "image" and isinstance(child, str) and child.strip():
                result.append({"source": prefix, "configured_image": child.strip()})
            elif key not in {"portal", "persistence"}:
                result.extend(_images(child, key_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            result.extend(_images(child, f"{prefix}[{index}]"))
    return result


def _versions(provision: dict[str, Any]) -> list[dict[str, Any]]:
    """Версии из Git — требуемые, не фактически установленные."""
    versions = _images(provision)
    photo = provision.get("photo_archive")
    if isinstance(photo, dict) and isinstance(photo.get("immich_version"), str):
        versions.append({
            "source": "photo_archive",
            "configured_image": f"immich:{photo['immich_version']}",
        })
    return [
        {
            **item,
            "installed_version": None,
            "available_version": None,
        }
        for item in versions
    ]


def guest_inventory(
    repo_root: Path,
    *,
    resources: list[dict[str, Any]] | None,
    pve_error: str | None = None,
) -> dict[str, Any]:
    """Объединить Git-каталог и PVE, не приписывая гостям ложную готовность."""
    guests = deployable_guests(repo_root)
    permitted = {
        operation: {guest.vmid for guest in operation_guests(repo_root, operation)}
        for operation in GUEST_OPERATIONS
    }
    pve: dict[int, dict[str, Any]] = {}
    if resources is not None:
        for raw in resources:
            if not isinstance(raw, dict) or raw.get("type") not in {"qemu", "lxc"}:
                continue
            try:
                vmid = int(raw["vmid"])
            except (KeyError, TypeError, ValueError):
                continue
            if vmid in pve:
                raise InfraManagerError(f"PVE вернул повторный VMID {vmid}")
            pve[vmid] = raw

    records: list[dict[str, Any]] = []
    for guest in guests:
        actual = pve.pop(guest.vmid, None)
        provision = _load_provision(guest)
        match = (
            actual is not None
            and actual.get("name") == guest.name
            and actual.get("type") == _expected_resource_type(repo_root, guest)
        )
        state = (
            "unknown" if resources is None
            else "absent" if actual is None
            else str(actual.get("status") or "unknown") if match
            else "mismatch"
        )
        links = [
            {"name": item["name"], "url": item["url"]}
            for item in portal_definitions(repo_root, guest)
            if "home" in item["targets"] or "local" in item["targets"]
        ]
        records.append({
            "vmid": guest.vmid,
            "name": guest.name,
            "kind": actual.get("type") if match else None,
            "state": state,
            "managed": True,
            "address": guest_management_address(repo_root, guest),
            "services": links,
            "versions": _versions(provision),
            "actions": (
                [operation for operation in GUEST_OPERATIONS if guest.vmid in permitted[operation]]
                if state != "mismatch" else []
            ),
        })
    for vmid, actual in sorted(pve.items()):
        records.append({
            "vmid": vmid,
            "name": str(actual.get("name") or f"VMID {vmid}"),
            "kind": str(actual.get("type") or ""),
            "state": str(actual.get("status") or "unknown"),
            "managed": False,
            "address": None,
            "services": [],
            "versions": [],
            "actions": [],
        })
    records.sort(key=lambda item: item["vmid"])
    return {
        "schema_version": 1,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "pve_available": resources is not None,
        "pve_error": pve_error,
        "guests": records,
    }


def read_guest_inventory(repo_root: Path) -> dict[str, Any]:
    """Получить единый снимок PVE; при отказе не выдавать старое состояние."""
    try:
        resources = PveClient().data("/cluster/resources", query={"type": "vm"})
        if not isinstance(resources, list):
            raise InfraManagerError("PVE вернул неверный формат списка ресурсов")
    except (InfraManagerError, OSError, ValueError) as exc:
        return guest_inventory(repo_root, resources=None, pve_error=str(exc))
    return guest_inventory(repo_root, resources=resources)

"""Поиск гостей с особыми системными ролями."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .common import InfraManagerError


@dataclass(frozen=True)
class GuestIdentity:
    vmid: int
    name: str
    role: str | None
    directory: Path
    manifest: Path
    source: dict[str, Any]

    @property
    def address(self) -> str | None:
        network = self.source.get("network")
        if not isinstance(network, dict):
            return None
        value = network.get("ipv4")
        return value if isinstance(value, str) and value != "dhcp" else None


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise InfraManagerError(f"Не удалось прочитать {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InfraManagerError(f"{path} должен содержать YAML object")
    return value


def guest_identity(repo_root: Path, vmid: int) -> GuestIdentity:
    """Прочитать идентичность одного гостя по VMID."""

    guests_root = repo_root / "infrastructure" / "guests"
    matches = sorted(
        path
        for path in guests_root.glob(f"{vmid}-*/guest.yaml")
        if path.is_file()
    )
    if len(matches) != 1:
        raise InfraManagerError(
            f"Для VMID {vmid} ожидается ровно один guest.yaml, "
            f"найдено: {len(matches)}"
        )

    manifest = matches[0]
    source = _load_manifest(manifest)
    actual_vmid = source.get("vmid")
    name = source.get("name")
    role = source.get("role")

    if actual_vmid != vmid:
        raise InfraManagerError(
            f"{manifest}: vmid {actual_vmid!r} не соответствует каталогу {vmid}"
        )
    if not isinstance(name, str) or not name:
        raise InfraManagerError(f"{manifest}: не задано корректное name")
    if role is not None and (not isinstance(role, str) or not role):
        raise InfraManagerError(f"{manifest}: задана некорректная role")

    return GuestIdentity(
        vmid=vmid,
        name=name,
        role=role,
        directory=manifest.parent,
        manifest=manifest,
        source=source,
    )


def find_guest_by_role(repo_root: Path, role: str) -> GuestIdentity:
    """Найти единственного гостя с указанной ролью."""

    matches: list[GuestIdentity] = []
    guests_root = repo_root / "infrastructure" / "guests"

    for manifest in sorted(guests_root.glob("*/guest.yaml")):
        source = _load_manifest(manifest)
        if source.get("role") != role:
            continue
        vmid = source.get("vmid")
        if not isinstance(vmid, int) or vmid <= 0:
            raise InfraManagerError(
                f"{manifest}: роль {role!r} имеет некорректный vmid"
            )
        matches.append(guest_identity(repo_root, vmid))

    if not matches:
        raise InfraManagerError(f"Не найден гость с ролью {role!r}")
    if len(matches) != 1:
        locations = ", ".join(str(item.manifest) for item in matches)
        raise InfraManagerError(
            f"Роль {role!r} должна принадлежать одному гостю: {locations}"
        )
    return matches[0]

"""Поиск гостей с особыми системными ролями."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

import yaml

from .common import InfraManagerError


@dataclass(frozen=True)
class GuestIdentity:
    """Проверенная принадлежность VMID каталогу и роли гостя.

    Содержит описание из guest.yaml, а не фактическое состояние PVE."""
    vmid: int
    name: str
    role: str | None
    directory: Path
    manifest: Path
    source: dict[str, Any]

    @property
    def address(self) -> str | None:
        """Вернуть административный IP из общего описания гостя."""
        return guest_management_address(self.directory.parents[2], self)


def load_manifest(path: Path) -> dict[str, Any]:
    """Прочитать YAML-объект машинного описания гостя."""
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
    source = load_manifest(manifest)
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


def deployable_guests(repo_root: Path) -> tuple[GuestIdentity, ...]:
    """Вернуть гостей, для которых есть guest.yaml и provision.yaml."""

    guests_root = repo_root / "infrastructure" / "guests"
    result: list[GuestIdentity] = []
    for manifest in sorted(guests_root.glob("*/guest.yaml")):
        if not (manifest.parent / "provision.yaml").is_file():
            continue
        source = load_manifest(manifest)
        vmid = source.get("vmid")
        if not isinstance(vmid, int) or vmid <= 0:
            raise InfraManagerError(
                f"{manifest}: некорректный vmid для развёртываемого гостя"
            )
        result.append(guest_identity(repo_root, vmid))
    return tuple(result)


def resolve_guest_configuration(
    repo_root: Path,
    source: dict[str, Any],
    defaults: dict[str, Any],
) -> tuple[dict[str, Any], str | None, str]:
    """Получить настройки и административный IP через единый resolver.py."""

    # Сценарии гостя и установленный пакет infra_manager находятся в разных
    # каталогах; корень переданной рабочей копии обеспечивает общий импорт.
    root = str(repo_root.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    from scripts.guests.resolver import GuestConfigError, resolve_effective_guest

    try:
        resolved = resolve_effective_guest(source, defaults)
    except GuestConfigError as exc:
        raise InfraManagerError(
            f"Некорректное описание гостя {source.get('vmid')}: {exc}"
        ) from exc

    address = (
        str(resolved.management_ip)
        if resolved.management_ip is not None
        else None
    )
    return resolved.effective, address, str(resolved.network.subnet)


def guest_management_address(
    repo_root: Path,
    identity: GuestIdentity,
) -> str | None:
    """Вернуть IP интерфейса management, не вычисляя адрес повторно."""

    defaults = load_manifest(
        repo_root / "infrastructure" / "guests" / "defaults.yaml"
    )
    _, address, _ = resolve_guest_configuration(
        repo_root, identity.source, defaults
    )
    return address


def find_guest_by_role(repo_root: Path, role: str) -> GuestIdentity:
    """Найти единственного гостя с указанной ролью."""

    matches: list[GuestIdentity] = []
    guests_root = repo_root / "infrastructure" / "guests"

    for manifest in sorted(guests_root.glob("*/guest.yaml")):
        source = load_manifest(manifest)
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

def guests_by_provision_section(
    repo_root: Path,
    section: str,
) -> tuple[GuestIdentity, ...]:
    """Вернуть гостей, чей provision.yaml содержит указанный раздел."""

    if not section:
        raise ValueError("section не должен быть пустым")

    guests_root = repo_root / "infrastructure" / "guests"
    matches: list[GuestIdentity] = []

    for provision in sorted(guests_root.glob("*/provision.yaml")):
        data = load_manifest(provision)
        if section not in data:
            continue

        vmid = data.get("guest_vmid")
        if not isinstance(vmid, int) or vmid <= 0:
            raise InfraManagerError(
                f"{provision}: раздел {section!r} связан с некорректным guest_vmid"
            )
        identity = guest_identity(repo_root, vmid)
        if identity.directory != provision.parent:
            raise InfraManagerError(
                f"{provision}: guest_vmid {vmid} указывает на другой каталог гостя"
            )
        matches.append(identity)

    return tuple(sorted(matches, key=lambda item: item.vmid))

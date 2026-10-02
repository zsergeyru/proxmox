#!/usr/bin/env python3
"""Поиск специальных гостей по явно заданной роли."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class GuestCatalogError(ValueError):
    """Каталог гостей не позволяет однозначно определить требуемую роль."""


@dataclass(frozen=True)
class GuestIdentity:
    vmid: int
    name: str
    role: str
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
        raise GuestCatalogError(f"Не удалось прочитать {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise GuestCatalogError(f"{path} должен содержать YAML object")
    return value


def find_guest_by_role(repo_root: Path, role: str) -> GuestIdentity:
    """Найти единственного гостя с указанной ролью."""

    matches: list[GuestIdentity] = []
    guests_root = repo_root / "infrastructure" / "guests"

    for manifest in sorted(guests_root.glob("*/guest.yaml")):
        source = _load_manifest(manifest)
        if source.get("role") != role:
            continue

        vmid = source.get("vmid")
        name = source.get("name")
        if not isinstance(vmid, int) or vmid <= 0:
            raise GuestCatalogError(
                f"{manifest}: роль {role!r} имеет некорректный vmid"
            )
        if not isinstance(name, str) or not name:
            raise GuestCatalogError(
                f"{manifest}: роль {role!r} имеет некорректное name"
            )

        matches.append(
            GuestIdentity(
                vmid=vmid,
                name=name,
                role=role,
                directory=manifest.parent,
                manifest=manifest,
                source=source,
            )
        )

    if not matches:
        raise GuestCatalogError(f"Не найден гость с ролью {role!r}")
    if len(matches) != 1:
        locations = ", ".join(str(item.manifest) for item in matches)
        raise GuestCatalogError(
            f"Роль {role!r} должна принадлежать одному гостю: {locations}"
        )
    return matches[0]

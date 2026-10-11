"""Поиск единственного управляющего гостя по роли из описания проекта.

Модуль запускается одинаково на PVE и внутри временного LXC: через импорт
функции или как самостоятельный сценарий. Для чтения простых верхнеуровневых
полей guest.yaml не требуется установка сторонних библиотек.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


class RoleLookupError(ValueError):
    """Описание гостя или состав ролей не удовлетворяет требованиям."""


def _plain_top_level_scalars(path: Path) -> dict[str, str]:
    """Прочитать верхнеуровневые скалярные поля гостя без PyYAML."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RoleLookupError(f"Не удалось прочитать {path}: {exc}") from exc

    for raw in lines:
        if not raw or raw[0].isspace() or raw.lstrip().startswith("#"):
            continue
        key, separator, value = raw.partition(":")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def find_role_guest(project_dir: Path, role: str) -> tuple[int, str]:
    """Найти единственного гостя заданной роли, сверив имя каталога и manifest."""
    guests_dir = project_dir / "infrastructure" / "guests"
    matches: list[tuple[int, str, Path]] = []

    for manifest in sorted(guests_dir.glob("*/guest.yaml")):
        values = _plain_top_level_scalars(manifest)
        if values.get("role") != role:
            continue

        directory_match = re.fullmatch(r"(\d{3})-(.+)", manifest.parent.name)
        if directory_match is None:
            raise RoleLookupError(
                f"Каталог гостя с ролью {role!r} имеет неверное имя: "
                f"{manifest.parent.name}"
            )
        directory_vmid = int(directory_match.group(1))
        directory_name = directory_match.group(2)
        try:
            manifest_vmid = int(values.get("vmid", ""))
        except ValueError as exc:
            raise RoleLookupError(
                f"{manifest}: роль {role!r} имеет некорректный vmid"
            ) from exc
        manifest_name = values.get("name", "")
        if manifest_vmid != directory_vmid or manifest_name != directory_name:
            raise RoleLookupError(
                f"{manifest}: vmid/name не соответствуют имени каталога"
            )
        matches.append((manifest_vmid, manifest_name, manifest))

    if not matches:
        raise RoleLookupError(f"Не найден гость с ролью {role!r}")
    if len(matches) != 1:
        locations = ", ".join(str(item[2]) for item in matches)
        raise RoleLookupError(
            f"Роль {role!r} должна принадлежать одному гостю: {locations}"
        )
    vmid, name, _ = matches[0]
    return vmid, name


def main() -> int:
    """Вывести идентификатор найденного гостя для вызывающего процесса."""
    parser = argparse.ArgumentParser(description="Поиск гостя по роли.")
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("role")
    args = parser.parse_args()
    try:
        vmid, name = find_role_guest(args.project_dir, args.role)
    except RoleLookupError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"vmid": vmid, "name": name}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

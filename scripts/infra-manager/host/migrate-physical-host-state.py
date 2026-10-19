#!/usr/bin/env python3
"""Бережное копирование старых подтверждений физических машин из locks/.

Вызывается Ansible на управляющем госте до запуска новой версии среды.
Не удаляет исходники, не изменяет конфликтующие записи и не использует NAS.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

_NAME = re.compile(r"^[a-z][a-z0-9-]*(?:-storage)?[.]json$")


class MigrationError(RuntimeError):
    """Нельзя безопасно принять старые данные."""


def _check_dir(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise MigrationError(f"Недопустимый каталог состояния: {path}")


def _read(path: Path) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise MigrationError(f"Запись не является обычным файлом: {path}")
    if info.st_size == 0 or info.st_size > 1024 * 1024:
        raise MigrationError(f"Недопустимый размер записи: {path}")
    payload = path.read_bytes()
    try:
        document = json.loads(payload)
    except (ValueError, UnicodeError) as exc:
        raise MigrationError(f"Повреждён JSON подтверждения: {path}") from exc
    name = path.name.removesuffix(".json")
    storage = name.endswith("-storage")
    machine = name.removesuffix("-storage") if storage else name
    if not isinstance(document, dict) or document.get("name") != machine:
        raise MigrationError(f"Запись не соответствует имени машины: {path}")
    if storage:
        if not isinstance(document.get("devices"), list) or not isinstance(
            document.get("volumes"), dict
        ) or not document.get("devices") or not document["volumes"]:
            raise MigrationError(f"Неполные сведения о дисках: {path}")
    else:
        if not isinstance(document.get("address"), str) or not isinstance(
            document.get("fingerprint"), str
        ):
            raise MigrationError(f"Неполное подтверждение SSH: {path}")
    return payload


def migrate(legacy: Path, destination: Path) -> int:
    """Копировать только подтверждённые записи; конфликт означает отказ."""

    _check_dir(legacy)
    _check_dir(destination)
    if not legacy.exists():
        return 0

    records = sorted(legacy.iterdir())
    for source in records:
        if not _NAME.fullmatch(source.name):
            raise MigrationError(f"Неожиданный файл в старом состоянии: {source}")
        data = _read(source)
        target = destination / source.name
        if target.is_symlink():
            raise MigrationError(f"Символьная ссылка вместо записи: {target}")
        if target.exists() and _read(target) != data:
            raise MigrationError(
                f"Старое и новое подтверждение отличаются: {target}. "
                "Ничего не перезаписано; требуется проверка"
            )

    if destination.exists() and destination.is_symlink():
        raise MigrationError("Целевой каталог является ссылкой")
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    if os.geteuid() == 0:
        os.chown(destination, 1001, 0)
    created = 0
    for source in records:
        target = destination / source.name
        if target.exists():
            continue
        data = _read(source)
        fd, tmp = tempfile.mkstemp(prefix=".physical-host-", dir=destination)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(tmp, 0o600)
            if os.geteuid() == 0:
                os.chown(tmp, 1001, 0)
            # Ссылка создаётся исключительно если имя ещё свободно:
            # ни при гонке процессов, ни при сбое нельзя заменить старую запись.
            os.link(tmp, target, follow_symlinks=False)
            created += 1
        except FileExistsError as exc:
            if _read(target) != data:
                raise MigrationError(f"Конфликт параллельной записи: {target}") from exc
        finally:
            Path(tmp).unlink(missing_ok=True)
    directory_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return created


def main() -> int:
    if len(sys.argv) != 3:
        print("Использование: migrate-physical-host-state.py OLD NEW", file=sys.stderr)
        return 2
    try:
        created = migrate(Path(sys.argv[1]), Path(sys.argv[2]))
    except (MigrationError, OSError) as exc:
        print(f"[ОШИБКА] Подтверждения физических машин: {exc}", file=sys.stderr)
        return 1
    print(f"[ОК] Записи физических машин проверены; скопировано: {created}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

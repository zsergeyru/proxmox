#!/usr/bin/env python3
"""Только чтение: краткое состояние архивов на PVE для оператора 910.

Не требует сторонних модулей, не включает службы, не обращается к сети,
не читает закрытые файлы и никогда не объявляет непроверенное восстановление.
В будущем расписанием и самими копиями управляет 910.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, date
from pathlib import Path

ROOT = Path("/var/lib/infra-manager/backup")
LAST_SUCCESS = ROOT / "last-success.json"
LAST_VM100 = ROOT / "last-vm100.json"
RESTORE_APPROVED = Path("/etc/infra-manager/backup/restore-tested")
KEY_EXPORTED = Path("/etc/infra-manager/backup/recovery-key-exported")
FAST_MOUNT = Path("/mnt/pve/backup-fast")
NAS_MOUNT = Path("/mnt/pve/nas-backup")
MAX_CORE_AGE_HOURS = 9
MAX_VM100_AGE_DAYS = 1


def _read_record(path: Path) -> dict | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _core_status(now: datetime) -> str:
    record = _read_record(LAST_SUCCESS)
    if not record:
        return "[ОЖИДАНИЕ] Нет подтверждённой полной копии PVE и 910"
    try:
        stamp = datetime.fromisoformat(record["created_utc"])
        targets = record["destinations"]
        if stamp.tzinfo is None or not isinstance(targets, list):
            raise ValueError("Некорректное время или назначения")
        required = {FAST_MOUNT, NAS_MOUNT}
        actual = {Path(value).parent for value in targets if isinstance(value, str)}
        if actual != required or len(targets) != 2:
            raise ValueError("Архивы не подтверждены в обоих независимых местах")
        age = (now - stamp).total_seconds() / 3600
        if age < -0.5:
            raise ValueError("Время архива находится в будущем")
    except (KeyError, TypeError, ValueError, OverflowError):
        return "[ПРЕДУПРЕЖДЕНИЕ] Запись о последней копии неполная или повреждена"
    if age > MAX_CORE_AGE_HOURS:
        return f"[ПРЕДУПРЕЖДЕНИЕ] Последняя копия устарела: {age:.1f} ч"
    if not FAST_MOUNT.is_mount() or not NAS_MOUNT.is_mount():
        return "[ПРЕДУПРЕЖДЕНИЕ] Последняя копия известна, но SSD или NAS сейчас не подключён"
    if not all(Path(value).is_dir() for value in targets):
        return "[ПРЕДУПРЕЖДЕНИЕ] Каталог одного из архивов недоступен"
    return (
        f"[ИНФО] Последняя полная копия: {stamp.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC; "
        "обе точки доступны, текущая целостность не перепроверена"
    )


def _vm100_status(now: datetime) -> str:
    record = _read_record(LAST_VM100)
    if not record:
        return "[ОЖИДАНИЕ] Нет подтверждения ежедневной копии VM 100"
    try:
        stamp = date.fromisoformat(record["date"])
        age = (now.date() - stamp).days
    except (KeyError, TypeError, ValueError):
        return "[ПРЕДУПРЕЖДЕНИЕ] Запись о VM 100 повреждена"
    if age < 0 or age > MAX_VM100_AGE_DAYS:
        return "[ПРЕДУПРЕЖДЕНИЕ] Ежедневная копия VM 100 устарела"
    return f"[ИНФО] Копия VM 100: {stamp.isoformat()}"


def report(now: datetime | None = None) -> str:
    """Отразить наблюдаемое состояние, а не обещание нового расписания."""
    current = now or datetime.now(timezone.utc)
    lines = [
        "Резервирование (только чтение, PVE-only):",
        _core_status(current),
        _vm100_status(current),
        (
            "[ИНФО] Ключ восстановления экспортирован ранее"
            if KEY_EXPORTED.is_file() and not KEY_EXPORTED.is_symlink()
            else "[ПРЕДУПРЕЖДЕНИЕ] Независимая копия ключа не подтверждена; "
                 "это не блокирует резервирование"
        ),
        (
            "[ИНФО] Испытание восстановления отмечено"
            if RESTORE_APPROVED.is_file() and not RESTORE_APPROVED.is_symlink()
            else "[ПРЕДУПРЕЖДЕНИЕ] Восстановление из архива не испытано"
        ),
        "[ИНФО] Расписание и пригодность архивов требуют отдельной проверки в 910",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())

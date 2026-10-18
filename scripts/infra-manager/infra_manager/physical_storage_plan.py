"""Безопасный план подготовки неразмеченных дисков физического NAS.

Назначение ролей выполняется по модели, ёмкости и серийному номеру.
План не меняет носители, не импортирует пулы и не создаёт файловые системы.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .common import InfraManagerError
from .physical_storage import DiskInspection


@dataclass(frozen=True)
class StorageAssignment:
    """Конкретный диск и назначенное действие без выполнения операции."""

    role: str
    serial: str
    model: str
    size: int
    action: str
    path_for_display: str


@dataclass(frozen=True)
class StoragePlan:
    """Однозначное решение на основании проверенного состава накопителей."""

    assignments: tuple[StorageAssignment, ...]
    fingerprint: str

    @property
    def requires_creation(self) -> bool:
        """Требуется ли подготовить хотя бы один новый пустой диск."""

        return any(item.action != "preserve" for item in self.assignments)


def _model_matches(expected: str, actual: str) -> bool:
    """Проверить имя модели без угадывания по объёму."""

    return expected.casefold() in actual.casefold()


def _size_matches(gigabytes: int, actual: int) -> bool:
    """Разрешить небольшую разницу между заявленным и реальным объёмом."""

    return abs(actual / 10**9 - gigabytes) <= gigabytes * 0.05


def propose_storage(
    devices: tuple[dict[str, object], ...],
    disks: tuple[DiskInspection, ...],
) -> StoragePlan:
    """Однозначно назначить ожидаемые HDD, но ничего не форматировать."""

    observed = [disk for disk in disks if disk.serial]
    if len({disk.serial for disk in observed}) != len(observed):
        raise InfraManagerError("Повторяются серийные номера физических дисков")
    if len([disk for disk in disks if disk.is_system]) != 1:
        raise InfraManagerError("Не подтверждён единственный системный SSD")

    claimed: set[str] = set()
    entries: list[StorageAssignment] = []
    for device in devices:
        role = device.get("role")
        if role == "unused":
            continue
        if role not in {"system", "shared", "data", "backup"}:
            raise InfraManagerError(f"Неизвестная роль диска: {role}")
        expected_serial = device.get("serial")
        expected_model = device.get("model")
        expected_gb = device.get("capacity_gb")
        if not isinstance(expected_model, str) or not isinstance(expected_gb, int):
            raise InfraManagerError(f"Не указаны модель и ёмкость диска: {role}")
        candidates = [
            disk for disk in observed
            if disk.serial not in claimed
            and (not expected_serial or disk.serial == expected_serial)
            and _model_matches(expected_model, disk.model)
            and _size_matches(expected_gb, disk.capacity_bytes)
            and disk.is_system == (role == "system")
        ]
        candidates.sort(key=lambda item: item.serial or "")
        if not candidates:
            raise InfraManagerError(
                f"Нет подходящего устройства для роли {role}. "
                "Не разрешается подбирать диск только по /dev/sdX"
            )
        if expected_serial is not None and len(candidates) != 1:
            raise InfraManagerError(f"Неоднозначная идентичность диска {role}")
        # Два одинаковых HDD назначаются по серийному номеру,
        # а затем конкретное соответствие обязательно показывается оператору.
        disk = candidates[0]
        assert disk.serial is not None
        claimed.add(disk.serial)
        if role == "system":
            if not disk.contains_existing_layout:
                raise InfraManagerError("Системный SSD не содержит разметку ОС")
            action = "preserve"
        elif disk.contains_existing_layout:
            action = "preserve"
        else:
            action = "create-ext4" if role == "shared" else "create-zfs"
        entries.append(StorageAssignment(
            role, disk.serial, disk.model,
            disk.capacity_bytes, action, disk.path,
        ))
    present = {entry.role for entry in entries}
    if present != {"system", "shared", "data", "backup"}:
        raise InfraManagerError("Не найдены все четыре предусмотренные роли NAS")
    payload = [
        {"role": e.role, "serial": e.serial, "model": e.model,
         "size": e.size, "action": e.action}
        for e in entries
    ]
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return StoragePlan(tuple(entries), fingerprint)


def format_storage_plan(plan: StoragePlan) -> str:
    """Предложить понятный оператору план с точными серийными номерами."""

    labels = {
        "system": "Системный SSD",
        "shared": "Общие файлы, ext4",
        "data": "Основные данные, ZFS nas-data",
        "backup": "Резервирование, ZFS nas-backup",
    }
    actions = {
        "preserve": "Сохранить без изменений",
        "create-ext4": "Подготовить ext4 на новом HDD",
        "create-zfs": "Создать независимый однодисковый пул ZFS",
    }
    lines = ["Предлагаемое назначение накопителей NAS:"]
    for entry in plan.assignments:
        lines.append(
            f"  {labels[entry.role]}: {entry.model} "
            f"({entry.size // 10**9} ГБ), номер {entry.serial}, "
            f"{actions[entry.action]}"
        )
    lines.append(f"Контрольный отпечаток плана: {plan.fingerprint}")
    lines.append(
        "Это только просмотр. Подготовка дисков не выполнялась."
    )
    if plan.requires_creation:
        lines.append(
            "Для применения потребуется отдельное подтверждение "
            "точных номеров и повторная проверка отсутствия данных."
        )
    return "\n".join(lines)

"""Чтение списка накопителей NAS без изменения таблиц разделов и пулов."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .common import InfraManagerError


@dataclass(frozen=True)
class DiskInspection:
    """Фактические сведения о диске из lsblk."""

    path: str
    model: str
    serial: str | None
    capacity_bytes: int
    is_system: bool
    contains_existing_layout: bool
    partition_table: str | None = None
    filesystem: str | None = None
    mountpoints: tuple[str, ...] = ()
    partitions: tuple[str, ...] = ()


def _mount_locations(device: dict[str, object]) -> tuple[str, ...]:
    """Показать только определённые lsblk точки подключения."""

    mounts = device.get("mountpoints")
    if mounts is None:
        return ()
    if not isinstance(mounts, list) or any(
        point is not None and not isinstance(point, str) for point in mounts
    ):
        raise InfraManagerError("lsblk вернул некорректные точки подключения")
    return tuple(point for point in mounts if isinstance(point, str) and point)


def _partitions(device: dict[str, object]) -> tuple[str, ...]:
    """Вывести существующие разделы и ФС с UUID без изменения диска."""

    result: list[str] = []
    children = device.get("children") or []
    if not isinstance(children, list):
        raise InfraManagerError("lsblk вернул некорректное дерево разделов")
    for child in children:
        if not isinstance(child, dict):
            raise InfraManagerError("lsblk вернул некорректный раздел")
        path = child.get("path")
        fs = child.get("fstype")
        uid = child.get("uuid")
        if not isinstance(path, str) or not path.startswith("/dev/"):
            raise InfraManagerError("Не определён путь существующего раздела")
        if fs is not None and not isinstance(fs, str):
            raise InfraManagerError("Некорректный тип файловой системы lsblk")
        if uid is not None and not isinstance(uid, str):
            raise InfraManagerError("Некорректный UUID lsblk")
        description = f"{path}: ФС {fs or 'нет'}"
        if uid:
            description += f", UUID {uid}"
        mounted = _mount_locations(child)
        if mounted:
            description += ", подключён: " + ", ".join(mounted)
        result.append(description)
        result.extend(_partitions(child))
    return tuple(result)


def _contains_root(device: dict[str, object]) -> bool:
    """Найти корневую файловую систему в дереве устройства."""

    mountpoints = device.get("mountpoints")
    if not isinstance(mountpoints, list):
        raise InfraManagerError("lsblk не вернул список точек подключения")
    if "/" in mountpoints:
        return True
    children = device.get("children", [])
    if not isinstance(children, list):
        raise InfraManagerError("Некорректное дерево разделов lsblk")
    return any(_contains_root(child) for child in children if isinstance(child, dict))


def _has_layout(device: dict[str, object]) -> bool:
    """Определить, есть ли у накопителя разделы, ФС или таблица разделов."""

    for field in ("fstype", "uuid", "pttype"):
        if device.get(field):
            return True
    children = device.get("children", [])
    if not isinstance(children, list):
        raise InfraManagerError("Некорректное дерево разделов lsblk")
    return bool(children)


def read_disk_inspection(raw_json: str) -> tuple[DiskInspection, ...]:
    """Проверить JSON lsblk и обнаружить системный диск без имён /dev/sdX."""

    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise InfraManagerError("lsblk вернул некорректный JSON") from exc
    items = payload.get("blockdevices") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise InfraManagerError("В выводе lsblk отсутствует blockdevices")
    disks: list[DiskInspection] = []
    for item in items:
        if not isinstance(item, dict):
            raise InfraManagerError("lsblk вернул некорректное устройство")
        if item.get("type") != "disk":
            continue
        path, model, size = item.get("path"), item.get("model"), item.get("size")
        if (
            not isinstance(path, str)
            or not path.startswith("/dev/")
            or not isinstance(model, str)
            or not isinstance(size, int)
            or size <= 0
        ):
            raise InfraManagerError("lsblk не смог надёжно определить физический диск")
        serial = item.get("serial")
        if serial is not None and not isinstance(serial, str):
            raise InfraManagerError("lsblk вернул некорректный серийный номер")
        table, fs = item.get("pttype"), item.get("fstype")
        if (table is not None and not isinstance(table, str)) or (
            fs is not None and not isinstance(fs, str)
        ):
            raise InfraManagerError("lsblk вернул некорректный тип разметки")
        disks.append(DiskInspection(
            path=path,
            model=model.strip(),
            serial=serial.strip() if serial and serial.strip() else None,
            capacity_bytes=size,
            is_system=_contains_root(item),
            contains_existing_layout=_has_layout(item),
            partition_table=table,
            filesystem=fs,
            mountpoints=_mount_locations(item),
            partitions=_partitions(item),
        ))
    if not disks or not any(disk.is_system for disk in disks):
        raise InfraManagerError(
            "Не удалось однозначно обнаружить системный диск; действия запрещены"
        )
    if sum(disk.is_system for disk in disks) != 1:
        raise InfraManagerError("Несколько дисков содержат корневую файловую систему")
    return tuple(disks)


def verify_expected_disks(
    devices: tuple[dict[str, object], ...],
    disks: tuple[DiskInspection, ...],
) -> None:
    """Сверить назначенные диски по номерам, модели, ёмкости и системной роли."""

    serials = [disk.serial for disk in disks if disk.serial]
    if len(serials) != len(set(serials)):
        raise InfraManagerError("Обнаружены несколько дисков с одинаковым серийным номером")
    observed = {disk.serial: disk for disk in disks if disk.serial}
    for device in devices:
        serial = device.get("serial")
        role = device.get("role")
        if not isinstance(serial, str) or not serial:
            raise InfraManagerError(f"Не подтверждён серийный номер для роли {role}")
        disk = observed.get(serial)
        if disk is None:
            raise InfraManagerError(f"Не найден назначенный диск {role}: {serial}")
        model = device.get("model")
        expected_size = device.get("capacity_gb")
        if (
            not isinstance(model, str)
            or model.lower() not in disk.model.lower()
            or not isinstance(expected_size, int)
            or abs(disk.capacity_bytes / 10**9 - expected_size) > expected_size * 0.05
        ):
            raise InfraManagerError(
                f"Диск {serial}: модель или ёмкость не соответствуют роли {role}"
            )
        if (role == "system") != disk.is_system:
            raise InfraManagerError(
                f"Диск {serial}: назначение system не совпадает с корневой ФС"
            )

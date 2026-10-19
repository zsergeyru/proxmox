#!/usr/bin/env python3
"""Проверка и одноразовая подготовка только ПУСТОГО резервного SSD на PVE.

inspect никогда не пишет. prepare требует явного подтверждения отпечатка
проверенного /dev/disk/by-id; при сомнении прекращает работу без очистки.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


class DeviceError(RuntimeError):
    """Не подтверждён резервный физический носитель."""


def execute(argv: list[str], *, data: str | None = None, check: bool = True):
    result = subprocess.run(
        argv, input=data, capture_output=True, text=True, check=False,
    )
    if check and result.returncode:
        raise DeviceError(f"Команда {argv[0]} завершилась с ошибкой; диск не изменять")
    return result


def inventory() -> list[dict]:
    result = execute([
        "lsblk", "--json", "-b", "-o",
        "PATH,TYPE,SIZE,MODEL,SERIAL,WWN,FSTYPE,UUID,PTTYPE,MOUNTPOINTS,RO,RM",
    ])
    payload = json.loads(result.stdout)
    if not isinstance(payload.get("blockdevices"), list):
        raise DeviceError("Не удалось получить структуру дисков")
    return payload["blockdevices"]


def _contains_mount(device: dict) -> bool:
    return bool(device.get("mountpoints") and any(device["mountpoints"]))


def inspect() -> None:
    print("Диски PVE (только чтение):")
    for item in inventory():
        if item.get("type") != "disk":
            continue
        print(
            f"  {item.get('path')} размер={item.get('size')} "
            f"модель={item.get('model')} серийный={item.get('serial')} "
            f"разделы={len(item.get('children') or [])} "
            f"ФС={item.get('fstype') or '-'}"
        )
    print("Ни один диск не был изменён")


def empty_disk(path: Path, serial: str) -> tuple[str, int]:
    """Отказать при любой разметке, сигнатуре, держателе, несоответствии."""

    if not str(path).startswith("/dev/disk/by-id/") or path.name.endswith("-part1"):
        raise DeviceError("Нужен подтверждённый путь целого диска /dev/disk/by-id")
    if path.is_dir() or not path.is_symlink():
        raise DeviceError("Нужна существующая ссылка /dev/disk/by-id на целый диск")
    real = path.resolve(strict=True)
    if not str(real).startswith("/dev/"):
        raise DeviceError("Путь ведёт за пределы /dev")
    result = execute([
        "lsblk", "--json", "-b", "-o",
        "PATH,TYPE,SIZE,MODEL,SERIAL,FSTYPE,UUID,PTTYPE,MOUNTPOINTS,RO,RM",
        str(real),
    ])
    devs = json.loads(result.stdout).get("blockdevices")
    if not isinstance(devs, list) or len(devs) != 1:
        raise DeviceError("Невозможно однозначно определить SSD")
    device = devs[0]
    size = device.get("size")
    if (
        device.get("type") != "disk"
        or not isinstance(size, int)
        or not 180_000_000_000 <= size <= 300_000_000_000
        or str(device.get("serial") or "").strip() != serial
        or device.get("ro") not in (0, False, "0")
        or device.get("children")
        or device.get("fstype")
        or device.get("uuid")
        or device.get("pttype")
        or _contains_mount(device)
    ):
        raise DeviceError("SSD не пустой, не того размера или его идентичность не подтверждена")
    if execute(["wipefs", "-n", str(real)]).stdout.strip():
        raise DeviceError("На диске есть сигнатуры: очистка запрещена")
    probe = execute(["blkid", "-p", str(real)], check=False)
    if probe.returncode != 2:
        raise DeviceError("Обнаружена сигнатура или неизвестная ошибка проверки")
    sysfs = Path("/sys/class/block") / real.name / "holders"
    if not sysfs.is_dir() or any(sysfs.iterdir()):
        raise DeviceError("Диск используется устройством-держателем")
    return str(real), size


def confirmation(path: Path, serial: str, size: int) -> str:
    raw = f"{path.resolve()}|{serial}|{size}".encode()
    return "ФОРМАТИРОВАТЬ-" + hashlib.sha256(raw).hexdigest()[:12].upper()


def prepare(path: Path, serial: str, confirm: str) -> None:
    real, size = empty_disk(path, serial)
    expected = confirmation(path, serial, size)
    if confirm != expected:
        raise DeviceError(f"Нет подтверждения. Для этого SSD требуется: {expected}")
    if os.geteuid() != 0:
        raise DeviceError("Разметка разрешена только root на физическом PVE")

    # Дополнительная проверка непосредственно перед первой записью.
    if empty_disk(path, serial) != (real, size):
        raise DeviceError("Характеристики диска изменились")
    execute(["sfdisk", "--label", "gpt", "--wipe", "never",
             "--wipe-partitions", "never", real], data=", , L\n")
    execute(["partprobe", real])
    execute(["udevadm", "settle"])
    partition = Path(str(path) + "-part1")
    for _ in range(15):
        if partition.exists():
            break
        time.sleep(1)
    if not partition.exists() or not partition.is_symlink():
        raise DeviceError("Раздел создан, но не найден по стабильной ссылке; не повторять разметку")
    info = execute(["lsblk", "-n", "-o", "TYPE,FSTYPE", str(partition)]).stdout.strip()
    if info != "part":
        raise DeviceError("Первый раздел не подтверждён как пустой; не форматировать")
    if execute(["wipefs", "-n", str(partition)]).stdout.strip():
        raise DeviceError("Новый раздел уже имеет сигнатуру; не форматировать")
    execute(["mkfs.ext4", "-L", "infra-backup", str(partition)])
    uuid = execute(["blkid", "-s", "UUID", "-o", "value", str(partition)]).stdout.strip()
    if not re.fullmatch(r"[a-fA-F0-9-]{36}", uuid):
        raise DeviceError("Файловая система создана, но UUID не определён")
    print(f"[ОК] SSD подготовлен; UUID={uuid}")
    print("Подключение и ключи резервирования выполняются отдельным шагом.")
    print("Если операция прервалась, её нельзя запускать повторно без новой проверки.")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("inspect")
    plan = sub.add_parser("plan")
    plan.add_argument("--disk", type=Path, required=True)
    plan.add_argument("--serial", required=True)
    apply = sub.add_parser("prepare")
    apply.add_argument("--disk", type=Path, required=True)
    apply.add_argument("--serial", required=True)
    apply.add_argument("--confirm", required=True)
    a = p.parse_args()
    try:
        if a.action == "inspect":
            inspect()
        elif a.action == "plan":
            _, size = empty_disk(a.disk, a.serial)
            print(f"[ОК] Диск пустой; размер={size}")
            print("Для явного подтверждения:", confirmation(a.disk, a.serial, size))
        else:
            prepare(a.disk, a.serial, a.confirm)
    except (DeviceError, OSError, ValueError, KeyError) as exc:
        print(f"[ОШИБКА] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

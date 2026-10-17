#!/usr/bin/env python3
"""Проверки чтения дисков NAS и маршрутизации физических команд."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "infra-manager"))

from infra_manager.common import InfraManagerError
from infra_manager.operator import build_parser
from infra_manager.physical_storage import read_disk_inspection, verify_expected_disks


def fixture() -> dict[str, object]:
    """Сформировать данные lsblk без реального оборудования."""

    return {
        "blockdevices": [
            {
                "type": "disk", "path": "/dev/sda", "model": "SPCC",
                "serial": "SYSTEM123", "size": 120000000000,
                "fstype": None, "uuid": None, "pttype": "gpt",
                "mountpoints": [None],
                "children": [
                    {"type": "part", "mountpoints": ["/"], "fstype": "ext4"}
                ],
            },
            {
                "type": "disk", "path": "/dev/sdb", "model": "Seagate",
                "serial": "DATA123", "size": 4000000000000,
                "fstype": None, "uuid": None, "pttype": None,
                "mountpoints": [None],
            },
        ]
    }


def test_disks() -> None:
    """Системный диск не должен предлагаться для подготовки."""

    disks = read_disk_inspection(json.dumps(fixture()))
    assert disks[0].is_system and disks[0].contains_existing_layout
    assert not disks[1].is_system
    assert not disks[1].contains_existing_layout


def test_missing_root() -> None:
    """Не продолжать работу при невозможности определить системный диск."""

    data = fixture()
    data["blockdevices"][0]["children"][0]["mountpoints"] = [None]
    try:
        read_disk_inspection(json.dumps(data))
    except InfraManagerError:
        return
    raise AssertionError("Отсутствие системного диска не вызвало отказ")


def test_existing_partition() -> None:
    """Не принимать разделённый диск за новый пустой накопитель."""

    data = fixture()
    data["blockdevices"][1]["children"] = [
        {"type": "part", "mountpoints": [None], "fstype": None}
    ]
    disks = read_disk_inspection(json.dumps(data))
    assert disks[1].contains_existing_layout


def test_assignments() -> None:
    """Проверить соответствие системного диска и постоянных номеров."""

    disks = read_disk_inspection(json.dumps(fixture()))
    devices = (
        {"role": "system", "model": "SPCC", "capacity_gb": 120, "serial": "SYSTEM123"},
        {"role": "data", "model": "Seagate", "capacity_gb": 4000, "serial": "DATA123"},
    )
    verify_expected_disks(devices, disks)
    wrong = (dict(devices[0], serial="DATA123"), dict(devices[1], serial="SYSTEM123"))
    try:
        verify_expected_disks(wrong, disks)
    except InfraManagerError:
        pass
    else:
        raise AssertionError("Ошибочное назначение системного диска принято")


def test_cli() -> None:
    """Проверить совместимость выбора VMID и физических целей."""

    parser = build_parser()
    for argv, command in (
        (["hosts"], "hosts"),
        (["inspect", "nas"], "inspect"),
        (["status", "nas"], "status"),
        (["deploy", "nas"], "deploy"),
        (["deploy", "910"], "deploy"),
        (["repair", "140"], "repair"),
    ):
        assert parser.parse_args(argv).command == command


def main() -> int:
    """Выполнить безопасные автономные проверки."""

    test_disks()
    test_missing_root()
    test_existing_partition()
    test_assignments()
    test_cli()
    print("[ОК] Чтение дисков и команды физических узлов проверены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Испытания безопасного назначения HDD без подключения NAS."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "infra-manager"))

from infra_manager.common import InfraManagerError
from infra_manager.physical_storage import DiskInspection, read_disk_inspection
from infra_manager.physical_storage_plan import propose_storage, format_storage_plan
from infra_manager.operator import build_parser


def sample():
    """Предоставить ОС, пустые HDD и идентичные по модели ZFS-диски."""

    disks = (
        DiskInspection("/dev/sda", "SPCC", "SSD", 120 * 10**9, True, True),
        DiskInspection("/dev/sdb", "ST1000DM003", "SHARED", 1000 * 10**9, False, False),
        DiskInspection("/dev/sdc", "ST4000VX016", "ZFS-B", 4000 * 10**9, False, False),
        DiskInspection("/dev/sdd", "ST4000VX016", "ZFS-A", 4000 * 10**9, False, False),
    )
    devices = (
        {"role": "system", "serial": None, "model": "SPCC", "capacity_gb": 120},
        {"role": "shared", "serial": None, "model": "ST1000DM003", "capacity_gb": 1000},
        {"role": "data", "serial": None, "model": "ST4000VX016", "capacity_gb": 4000},
        {"role": "backup", "serial": None, "model": "ST4000VX016", "capacity_gb": 4000},
    )
    return devices, disks


def refuses(callback):
    """Проверить остановку при неоднозначной аппаратной конфигурации."""

    try:
        callback()
    except InfraManagerError:
        return
    raise AssertionError("Небезопасный план принят")


def test_observed_disk_layout() -> None:
    """Показать реальную GPT/ext4 на 1 ТБ и отсутствие разметки на новых HDD."""

    devices, _ = sample()
    raw = {
        "blockdevices": [
            {
                "path": "/dev/sda", "type": "disk", "model": "SPCC",
                "serial": "SSD", "size": 120 * 10**9, "pttype": "gpt",
                "mountpoints": [None], "children": [{
                    "path": "/dev/sda1", "type": "part", "fstype": "ext4",
                    "mountpoints": ["/"],
                }],
            },
            {
                "path": "/dev/sdb", "type": "disk", "model": "ST1000DM003",
                "serial": "SHARED", "size": 1000 * 10**9, "pttype": "gpt",
                "mountpoints": [None], "children": [{
                    "path": "/dev/sdb1", "type": "part", "fstype": "ext4",
                    "uuid": "abcd-1234", "mountpoints": ["/srv/shared"],
                }],
            },
            *[
                {
                    "path": path, "type": "disk", "model": "ST4000VX016",
                    "serial": serial, "size": 4000 * 10**9,
                    "pttype": None, "fstype": None,
                    "mountpoints": [None], "children": [],
                }
                for path, serial in (
                    ("/dev/sdc", "ZFS-A"), ("/dev/sdd", "ZFS-B")
                )
            ],
        ],
    }
    disks = read_disk_inspection(json.dumps(raw))
    plan = propose_storage(devices, disks)
    displayed = format_storage_plan(plan)
    assert "таблица разделов: gpt" in displayed
    assert "/dev/sdb1: ФС ext4, UUID abcd-1234, подключён: /srv/shared" in displayed
    assert "Устройство сейчас /dev/sdc; разметка, разделы и файловые системы не обнаружены" in displayed
    assert "Устройство сейчас /dev/sdd; разметка, разделы и файловые системы не обнаружены" in displayed


def main():
    """Проверить привязку ролей к номерам и защиту действующих разделов."""

    test_observed_disk_layout()
    devices, disks = sample()
    plan = propose_storage(devices, disks)
    assert len(plan.assignments) == 4
    assert [item.action for item in plan.assignments] == [
        "preserve", "create-ext4", "create-zfs", "create-zfs"
    ]
    assert plan.assignments[2].serial == "ZFS-A"
    assert plan.assignments[3].serial == "ZFS-B"
    assert plan.requires_creation
    assert len(plan.fingerprint) == 64
    assert "SSD" in format_storage_plan(plan)
    swapped = tuple(reversed(disks))
    assert propose_storage(devices, swapped).fingerprint == plan.fingerprint
    with_layout = tuple(
        d if d.serial != "SHARED" else DiskInspection(
            d.path, d.model, d.serial, d.capacity_bytes, False, True
        )
        for d in disks
    )
    existing = propose_storage(devices, with_layout)
    assert existing.assignments[1].action == "preserve"
    assert existing.fingerprint != plan.fingerprint
    refuses(lambda: propose_storage(devices, disks[:3]))
    refuses(lambda: propose_storage(devices, disks + (disks[1],)))
    refuses(lambda: propose_storage(devices, tuple(
        d if d.serial != "SSD" else DiskInspection(
            d.path, d.model, d.serial, d.capacity_bytes, True, False
        )
        for d in disks
    )))
    assert build_parser().parse_args(["deploy", "nas", "--_physical_stage", "plan"]).vmid == "nas"
    print("[ОК] План назначения дисков NAS проверен без форматирования")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

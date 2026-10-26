#!/usr/bin/env python3
"""Проверки чтения дисков NAS и маршрутизации физических команд."""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "infra-manager"))

from infra_manager.common import InfraManagerError
from infra_manager.operator import build_parser
from infra_manager.physical_hosts import load_physical_host
from infra_manager.physical_host_ssh import _scan_verified_key
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
                    {"type": "part", "path": "/dev/sda1", "mountpoints": ["/"], "fstype": "ext4"}
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
        {"type": "part", "path": "/dev/sdb1", "mountpoints": [None], "fstype": None}
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


def test_ssh_fingerprint() -> None:
    """Разрешать сетевой ключ лишь после сравнения с закреплённым отпечатком."""

    host = replace(
        load_physical_host(ROOT, "nas"),
        address="192.0.2.20",
        ssh_host_fingerprint="SHA256:" + "A" * 43,
    )

    def fake_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        """Подменить только чтение публичных ключей, не обращаться в сеть."""

        if argv[0] == "ssh-keyscan":
            return SimpleNamespace(
                returncode=0,
                stdout="192.0.2.20 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAAAA\\n",
            )
        if argv[0] == "ssh-keygen":
            return SimpleNamespace(
                returncode=0,
                stdout="256 SHA256:" + "A" * 43 + " host (ED25519)\\n",
            )
        raise AssertionError("Запрошена посторонняя команда")

    with tempfile.TemporaryDirectory() as temporary:
        with patch("infra_manager.physical_host_ssh.run", side_effect=fake_run):
            known_hosts = _scan_verified_key(host, Path(temporary))
            assert "ssh-ed25519" in known_hosts.read_text(encoding="utf-8")
        wrong = replace(host, ssh_host_fingerprint="SHA256:" + "B" * 43)
        with patch("infra_manager.physical_host_ssh.run", side_effect=fake_run):
            try:
                _scan_verified_key(wrong, Path(temporary))
            except InfraManagerError:
                pass
            else:
                raise AssertionError("Чужой SSH-ключ принят автоматически")


def test_cli() -> None:
    """Проверить совместимость выбора VMID и физических целей."""

    parser = build_parser()
    for argv, command in (
        (["hosts"], "hosts"),
        (["deploy", "nas", "--_physical_stage", "plan"], "deploy"),
        (["status", "nas"], "status"),
        (["deploy", "nas"], "deploy"),
        (["deploy", "910"], "deploy"),
        (["repair", "140"], "repair"),
    ):
        assert parser.parse_args(argv).command == command

    # Inspect and disk planning are mandatory internal phases of deploy,
    # not separately callable infra-manager commands.
    for legacy in ("inspect", "storage-plan", "storage-prepare"):
        try:
            parser.parse_args([legacy, "nas"])
        except SystemExit as exc:
            assert exc.code == 2
        else:
            raise AssertionError("Standalone command reintroduced: " + legacy)
    for step in ("plan", "prepare"):
        args = parser.parse_args(["deploy", "nas", "--_physical_stage", step])
        assert args.command == "deploy"
        assert args._physical_stage == step


def main() -> int:
    """Выполнить безопасные автономные проверки."""

    test_disks()
    test_missing_root()
    test_existing_partition()
    test_assignments()
    test_ssh_fingerprint()
    test_cli()
    print("[ОК] Чтение дисков и команды физических узлов проверены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

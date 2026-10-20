#!/usr/bin/env python3
"""Безопасная проверка подготовки SSD PVE без доступа к реальным дискам."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))
from infra_manager import pve_backup_ssd as ssd
from infra_manager.common import InfraManagerError


def disk(serial="19386J473508"):
    return {
        "devpath": "/dev/sda", "type": "ssd", "used": None, "gpt": 0,
        "mounted": False, "serial": serial, "model": "WDC WDS240G2G0A",
        "size": 240057409536,
    }


class FakeClient:
    def __init__(self):
        self.tasks = []

    def run_task(self, *args, **kwargs):
        self.tasks.append((args, kwargs))


def refuses(task, message):
    try:
        task()
    except InfraManagerError:
        return
    raise AssertionError(message)


def test_empty_disk_only_and_no_auto_wipe():
    assert ssd._eligible(disk(), 200 * 10**9) is not None
    for change in (
        {"used": "LVM"},
        {"mounted": True},
        {"gpt": 1},
        {"parent": "/dev/sda"},
        {"serial": ""},
        {"devpath": "/dev/mapper/pve-root"},
        {"type": "hdd"},
    ):
        assert ssd._eligible(dict(disk(), **change), 200 * 10**9) is None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "disk.json"
        with patch.object(ssd, "_state_path", return_value=path), patch.object(
            ssd, "load_physical_storage",
            return_value=SimpleNamespace(devices=[{
                "id": "backup_ssd", "role": "backup", "capacity_gb": 240,
            }]),
        ), patch.object(ssd, "_directory", return_value=None), patch.object(
            ssd, "_inventory", return_value=[disk()],
        ):
            client = FakeClient()
            assert ssd.reconcile_backup_ssd(client, "pve", interactive=False) is None
            assert client.tasks == []
            assert not path.exists()


def test_exact_confirmation_and_recheck():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "disk.json"
        mounted = [None, {"path": ssd.MOUNT, "type": "ext4"}]
        with (
            patch.object(ssd, "_state_path", return_value=path),
            patch.object(ssd, "load_physical_storage", return_value=SimpleNamespace(
                devices=[{"id": "backup_ssd", "role": "backup", "capacity_gb": 240}]
            )),
            patch.object(ssd, "_inventory", return_value=[disk()]) as inventory,
            patch.object(ssd, "_directory", side_effect=lambda *_: mounted[0]),
            patch.object(ssd, "_mount_info", return_value=("12345678-1234-1234-1234-1234567890ab", "ext4")),
        ):
            class Client(FakeClient):
                def run_task(self, *args, **kwargs):
                    super().run_task(*args, **kwargs)
                    mounted[0] = mounted[1]
            client = Client()
            assert ssd.reconcile_backup_ssd(
                client, "pve", interactive=True, confirmation=lambda *_: "WRONG"
            ) is None
            assert not client.tasks
            assert ssd.reconcile_backup_ssd(
                client, "pve", interactive=True, confirmation=lambda *_: "19386J473508"
            ) == "12345678-1234-1234-1234-1234567890ab"
            assert len(client.tasks) == 1
            args, kwargs = client.tasks[0]
            assert args == ("POST", "/nodes/pve/disks/directory")
            assert kwargs["form"] == {
                "device": "/dev/sda", "name": "backup-fast",
                "filesystem": "ext4", "add_storage": 0,
            }
            assert inventory.call_count >= 3
            saved = json.loads(path.read_text())
            assert saved["serial"] == "19386J473508"
            assert saved["uuid"] == "12345678-1234-1234-1234-1234567890ab"
            assert ssd.reconcile_backup_ssd(client, "pve") == saved["uuid"]
            assert len(client.tasks) == 1, "Повторный deploy не форматирует SSD"


def test_changed_device_rejected_before_write():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "disk.json"
        items = iter(([disk()], [disk("REPLACED99")]))
        with (
            patch.object(ssd, "_state_path", return_value=path),
            patch.object(ssd, "load_physical_storage", return_value=SimpleNamespace(
                devices=[{"id": "backup_ssd", "role": "backup", "capacity_gb": 240}]
            )),
            patch.object(ssd, "_inventory", side_effect=lambda *_: next(items)),
            patch.object(ssd, "_directory", return_value=None),
        ):
            client = FakeClient()
            refuses(lambda: ssd.reconcile_backup_ssd(
                client, "pve", interactive=True, confirmation=lambda *_: "19386J473508"
            ), "Подмена SSD после подтверждения должна остановить разметку")
            assert not client.tasks
            assert not path.exists()


if __name__ == "__main__":
    test_empty_disk_only_and_no_auto_wipe()
    test_exact_confirmation_and_recheck()
    test_changed_device_rejected_before_write()
    print("[ОК] SSD создаётся только через Proxmox API после точного подтверждения")

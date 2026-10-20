#!/usr/bin/env python3
"""Безопасная проверка подготовки SSD PVE без доступа к реальным дискам."""

from __future__ import annotations

import json
from contextlib import nullcontext
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
        with patch.object(ssd, "_empty_block_device"), patch.object(ssd, "_state_path", return_value=path), patch.object(
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
            patch.object(ssd, "_empty_block_device"),
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
            for response in ("", "нет", "Нет", "y", "19386J473508", "WRONG"):
                assert ssd.reconcile_backup_ssd(
                    client, "pve", interactive=True,
                    confirmation=lambda *_: response,
                ) is None
                assert not client.tasks
            assert ssd.reconcile_backup_ssd(
                client, "pve", interactive=True, confirmation=lambda *_: " ДА "
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
                client, "pve", interactive=True, confirmation=lambda *_: "да"
            ), "Подмена SSD после подтверждения должна остановить разметку")
            assert not client.tasks
            assert not path.exists()


def test_readonly_disk_refuses_partitions_and_signatures():
    candidate = ssd.Disk("/dev/sda", "19386J473508", "WD Green", 240057409536)
    valid = {
        "blockdevices": [{
            "path": "/dev/sda", "type": "disk",
            "size": candidate.size, "ro": False,
            "fstype": None, "mountpoints": [None],
        }]
    }
    def checked(result):
        with (
            patch.object(ssd, "_ssh_command", side_effect=lambda _, args: list(args)),
            patch.object(ssd, "run", side_effect=result),
        ):
            ssd._empty_block_device("pve", candidate)
    def fake(*args, **kwargs):
        if args[0][0] == "lsblk":
            return SimpleNamespace(returncode=0, stdout=json.dumps(valid))
        return SimpleNamespace(returncode=0, stdout="")
    checked(fake)
    valid["blockdevices"][0]["children"] = [{"path": "/dev/sda1"}]
    refuses(lambda: checked(fake), "Диск с разделами не является пустым")
    valid["blockdevices"][0].pop("children")
    def signature(*args, **kwargs):
        if args[0][0] == "lsblk":
            return SimpleNamespace(returncode=0, stdout=json.dumps(valid))
        return SimpleNamespace(returncode=0, stdout="ext4 0x123")
    refuses(lambda: checked(signature), "Подозрительная сигнатура wipefs блокирует mkfs")


def test_web_confirmation_starts_background_and_survives_browser():
    """Подтверждение запускает systemd сразу, второй deploy не нужен."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ssd-job.json"
        client = FakeClient()
        current = {
            "state": "needs-confirmation", "plan_id": "a" * 64,
            "serial": "19386J473508",
        }
        with (
            patch.object(ssd, "_job_path", return_value=path),
            patch.object(ssd, "inspect_backup_ssd", return_value=current),
            patch.object(ssd, "run", return_value=SimpleNamespace(returncode=0)) as launch,
        ):
            job_id = ssd.approve_backup_ssd(
                client, "pve", plan_id="a" * 64, serial="19386J473508",
            )
            assert len(job_id) == 32
            assert ssd.storage_status(client, "pve")["state"] == "running"
            assert launch.call_count == 1
            assert "systemd-run" in launch.call_args.args[0]
            assert client.tasks == [], "Никакой mkfs внутри HTTP подтверждения"
            refuses(lambda: ssd.approve_backup_ssd(
                client, "pve", plan_id="a" * 64, serial="19386J473508",
            ), "Повторное подтверждение во время операции запрещено")
            with (
                patch("infra_manager.operator._pve_node", return_value="pve"),
                patch.object(ssd, "PveClient", return_value=client),
                patch.object(ssd, "reconcile_backup_ssd",
                             return_value="12345678-1234-1234-1234-1234567890ab") as applied,
            ):
                assert ssd.apply_approved_backup_ssd(
                    job_id, "a" * 64, "19386J473508",
                ) == 0
            assert applied.call_count == 1
            assert json.loads(path.read_text())["state"] == "completed"
            refuses(lambda: ssd.apply_approved_backup_ssd(
                job_id, "a" * 64, "19386J473508",
            ), "Нельзя повторно применять завершённое задание")
            with patch.object(ssd, "inspect_backup_ssd", return_value={
                "state": "configured", "uuid": "12345678-1234-1234-1234-1234567890ab",
            }):
                assert ssd.storage_status(client, "pve")["state"] == "configured"


def test_ssh_preflight_confirmation_and_unattended_mode():
    """SSH с терминалом подтверждает SSD, автоматизация не форматирует."""
    client = FakeClient()
    with (
        patch("infra_manager.operator._pve_node", return_value="pve"),
        patch.object(ssd, "PveClient", return_value=client),
        patch.object(ssd, "_job_lock", side_effect=nullcontext),
        patch.object(ssd, "_read_job", return_value=None),
        patch.object(ssd, "reconcile_backup_ssd") as prepare,
    ):
        with patch.dict(ssd.os.environ, {"INFRA_MANAGER_INTERACTIVE": "1"}):
            ssd.deploy_backup_ssd_preflight()
        prepare.assert_called_once_with(client, "pve", interactive=True)

        prepare.reset_mock()
        with patch.dict(ssd.os.environ, {"INFRA_MANAGER_INTERACTIVE": "0"}):
            ssd.deploy_backup_ssd_preflight()
        prepare.assert_called_once_with(client, "pve", interactive=False)

        prepare.reset_mock()
        with (
            patch.dict(ssd.os.environ, {"INFRA_MANAGER_INTERACTIVE": "1"}),
            patch.object(ssd, "_read_job", return_value={"state": "running"}),
        ):
            ssd.deploy_backup_ssd_preflight()
        prepare.assert_not_called()

        prepare.reset_mock()
        with (
            patch.dict(ssd.os.environ, {"INFRA_MANAGER_INTERACTIVE": "1"}),
            patch.object(ssd, "_read_job", return_value=None),
            patch.object(ssd, "_job_lock", side_effect=ssd.InfraManagerError("busy")),
        ):
            ssd.deploy_backup_ssd_preflight()
        prepare.assert_not_called()


if __name__ == "__main__":
    test_empty_disk_only_and_no_auto_wipe()
    test_exact_confirmation_and_recheck()
    test_changed_device_rejected_before_write()
    test_readonly_disk_refuses_partitions_and_signatures()
    test_web_confirmation_starts_background_and_survives_browser()
    test_ssh_preflight_confirmation_and_unattended_mode()
    print("[ОК] SSD создаётся только через Proxmox API после точного подтверждения")

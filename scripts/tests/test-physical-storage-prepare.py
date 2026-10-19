#!/usr/bin/env python3
"""Проверки подтверждения HDD, повторной сверки и сохранности данных."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))
sys.path.insert(0, str(ROOT / "scripts/infra-manager/host"))

from infra_manager.common import InfraManagerError
from infra_manager.physical_hosts import load_physical_host
from infra_manager.physical_nas_storage_prepare import _check_result
from infra_manager.physical_nas_storage_state import (
    apply_saved_storage, save_storage_identity,
)
from infra_manager.physical_storage import DiskInspection
from infra_manager.physical_storage_plan import propose_storage
from infra_manager_nas_storage import confirm_nas_storage


def _load_remote():
    """Прочитать точно тот сценарий, который Ansible установит на NAS."""

    path = ROOT / "automation/ansible/files/nas-storage-prepare.py"
    spec = importlib.util.spec_from_file_location("nas_storage_prepare_script", path)
    assert spec and spec.loader
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    return script


def refuses(callback, exception=Exception):
    """Проверить безопасную остановку действий."""

    try:
        callback()
    except exception:
        return
    raise AssertionError("Опасная операция была разрешена")


def sample_plan():
    """Представить пустые три HDD и системный SSD."""

    host = load_physical_host(ROOT, "nas")
    disks = (
        DiskInspection("/dev/sda", "SPCC", "SYSTEM-TEST", 120_000_000_000, True, True),
        DiskInspection("/dev/sdb", "ST1000DM003", "SHARED-TEST", 1_000_000_000_000, False, False),
        DiskInspection("/dev/sdc", "ST4000VX016", "ZFS-A", 4_000_000_000_000, False, False),
        DiskInspection("/dev/sdd", "ST4000VX016", "ZFS-B", 4_000_000_000_000, False, False),
    )
    return host, propose_storage(host.devices, disks)


def test_confirmation():
    """Без подтверждения ни одно задание подготовки не отправляется на NAS."""

    _host, plan = sample_plan()
    responses = []
    output = {
        "fingerprint": plan.fingerprint, "requires_creation": True,
        "items": [
            {"role": item.role, "serial": item.serial, "action": item.action}
            for item in plan.assignments
        ],
    }

    def forward(command, *, capture_output=False, input_text=None):
        """Перехватить только доверенные вызовы в управляющий гость."""

        responses.append((command, input_text))
        if capture_output:
            return SimpleNamespace(
                returncode=0, stderr="",
                stdout="План пустых HDD\nNAS_STORAGE_PLAN_JSON="
                + json.dumps(output) + "\n",
            )
        return 0

    with (
        patch("infra_manager_nas_storage.sys.stdin",
              SimpleNamespace(isatty=lambda: True)),
        patch("builtins.input", return_value="ОТМЕНА"),
    ):
        refuses(lambda: confirm_nas_storage("nas", forward, RuntimeError), RuntimeError)
    assert all(command[0][0] != "storage-prepare" for command in responses)

    responses.clear()
    confirmation = "ПОДГОТОВИТЬ " + plan.fingerprint[-12:]
    with (
        patch("infra_manager_nas_storage.sys.stdin",
              SimpleNamespace(isatty=lambda: True)),
        patch("builtins.input", return_value=confirmation),
    ):
        assert confirm_nas_storage("nas", forward, RuntimeError) == 0
    assert responses[-1][0] == ["storage-prepare", "nas"]
    assert json.loads(responses[-1][1]) == {"fingerprint": plan.fingerprint}


def test_storage_state():
    """Состояние сохраняется однократно и отказывает при смене идентичности."""

    host, plan = sample_plan()
    host = replace(host, address="192.0.2.21",
                   ssh_host_fingerprint="SHA256:" + "A" * 43)
    volumes = {
        "shared": {"mountpoint": "/srv/dev-disk-by-uuid-" + "a" * 36,
                   "filesystem_uuid": "a" * 36, "omv_reference": "b" * 36},
        "data": {"mountpoint": "/nas-data", "pool": "nas-data",
                 "pool_guid": "123", "omv_reference": "c" * 36},
        "backup": {"mountpoint": "/nas-backup", "pool": "nas-backup",
                   "pool_guid": "456", "omv_reference": "d" * 36},
    }
    data = {"fingerprint": plan.fingerprint, "volumes": volumes,
            "serials": {item.role: item.serial for item in plan.assignments
                        if item.role != "system"}}
    complete = _check_result(host, plan, data)
    from infra_manager import physical_nas_storage_state as state
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder) / "physical-hosts").mkdir()
        with patch.object(state, "PATHS", SimpleNamespace(data_dir=Path(folder))):
            save_storage_identity(host, list(complete.devices), complete.volumes)
            loaded = apply_saved_storage(host)
            assert loaded.devices == complete.devices
            assert loaded.volumes == volumes
            refuses(lambda: save_storage_identity(host,
                                                  list(complete.devices), volumes),
                    InfraManagerError)
            refuses(lambda: apply_saved_storage(
                replace(host, address="192.0.2.23")), InfraManagerError)
    refuses(lambda: _check_result(host, plan, {**data,
            "serials": {**data["serials"], "backup": "OTHER"}}), InfraManagerError)


def test_remote_preflight():
    """Исполнитель не создаёт разделы без отсутствия сигнатур и by-id."""

    script = _load_remote()
    items = [
        {"role": "shared", "serial": "SHARED-TEST", "action": "create-ext4"},
        {"role": "data", "serial": "ZFS-A", "action": "create-zfs"},
        {"role": "backup", "serial": "ZFS-B", "action": "create-zfs"},
    ]
    disks = [
        {"path": "/dev/sda", "type": "disk", "serial": "SYSTEM-TEST",
         "model": "SPCC", "size": 120_000_000_000,
         "mountpoints": [None], "children": [
             {"mountpoints": ["/"], "children": []}]},
        {"path": "/dev/sdb", "type": "disk", "serial": "SHARED-TEST",
         "model": "ST1000DM003", "size": 1_000_000_000_000,
         "pttype": None, "mountpoints": [None], "children": []},
        {"path": "/dev/sdc", "type": "disk", "serial": "ZFS-A",
         "model": "ST4000VX016", "size": 4_000_000_000_000,
         "pttype": None, "mountpoints": [None], "children": []},
        {"path": "/dev/sdd", "type": "disk", "serial": "ZFS-B",
         "model": "ST4000VX016", "size": 4_000_000_000_000,
         "pttype": None, "mountpoints": [None], "children": []},
    ]

    def no_signatures(*args, **_kwargs):
        """Разрешить только чтение пустых накопителей."""
        if args[0] == "wipefs":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "blkid":
            return SimpleNamespace(returncode=2, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    with (
        patch.object(script, "disk_list", return_value=disks),
        patch.object(script, "stable_device",
                     side_effect=lambda path, _serial: "/dev/disk/by-id/" + path[-3:]),
        patch.object(script, "execute", side_effect=no_signatures),
    ):
        matched = script.preflight(items)
        assert len(matched) == 3
    disks[1]["children"] = [{"type": "part"}]
    with (
        patch.object(script, "disk_list", return_value=disks),
        patch.object(script, "stable_device", return_value="/dev/disk/by-id/test"),
        patch.object(script, "execute") as execute,
    ):
        refuses(lambda: script.preflight(items), script.StorageError)
        execute.assert_not_called()
    disks[1]["children"] = []
    with patch.object(script, "disk_list", return_value=disks):
        refuses(lambda: script.preflight(items[:2]), script.StorageError)


def main():
    """Проверить все контракты без обращения к реальным HDD."""

    test_confirmation()
    test_storage_state()
    test_remote_preflight()
    print("[ОК] Подтверждение и подготовка HDD проверены на искусственных данных")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

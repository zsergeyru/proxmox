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
from infra_manager.physical_storage_plan import propose_storage, observed_disk_condition
from infra_manager_nas_storage import confirm_nas_storage


def _load_remote():
    """Прочитать точно тот сценарий, который Ansible установит на NAS."""

    path = ROOT / "automation/ansible/roles/nas/files/nas-storage-prepare.py"
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
            {"role": item.role, "serial": item.serial, "action": item.action,
             "current_state": observed_disk_condition(item)}
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

    for response in ("", "нет", "ОТМЕНА", "y", "ПОДГОТОВИТЬ " + plan.fingerprint[-12:]):
        with (
            patch("infra_manager_nas_storage.sys.stdin",
                  SimpleNamespace(isatty=lambda: True)),
            patch("builtins.input", return_value=response),
        ):
            refuses(lambda: confirm_nas_storage("nas", forward, RuntimeError), RuntimeError)
        assert all(command[0] != ["deploy", "nas", "--_physical_stage", "prepare"] for command in responses)

    responses.clear()
    with (
        patch("infra_manager_nas_storage.sys.stdin",
              SimpleNamespace(isatty=lambda: True)),
        patch("builtins.input", return_value=" ДА "),
    ):
        assert confirm_nas_storage("nas", forward, RuntimeError) == 0
    assert responses[-1][0] == ["deploy", "nas", "--_physical_stage", "prepare"]
    assert json.loads(responses[-1][1]) == {"fingerprint": plan.fingerprint}


def test_existing_shared_confirmation() -> None:
    """План с существующим ext4 сохраняет HDD 1 ТБ и предлагает только ZFS."""

    host, _ = sample_plan()
    disks = (
        DiskInspection("/dev/sda", "SPCC", "SYSTEM-TEST", 120_000_000_000, True, True),
        DiskInspection("/dev/sdb", "ST1000DM003", "SHARED-TEST", 1_000_000_000_000, False, True),
        DiskInspection("/dev/sdc", "ST4000VX016", "ZFS-A", 4_000_000_000_000, False, False),
        DiskInspection("/dev/sdd", "ST4000VX016", "ZFS-B", 4_000_000_000_000, False, False),
    )
    plan = propose_storage(host.devices, disks)
    assert [item.action for item in plan.assignments] == [
        "preserve", "preserve", "create-zfs", "create-zfs",
    ]
    output = {
        "fingerprint": plan.fingerprint, "requires_creation": True,
        "items": [
            {"role": item.role, "serial": item.serial, "action": item.action,
             "current_state": observed_disk_condition(item)}
            for item in plan.assignments
        ],
    }
    calls = []

    def forward(command, *, capture_output=False, input_text=None):
        calls.append((command, input_text))
        if capture_output:
            return SimpleNamespace(
                returncode=0, stderr="",
                stdout="Plan\nNAS_STORAGE_PLAN_JSON=" + json.dumps(output) + "\n",
            )
        return 0

    with (
        patch("infra_manager_nas_storage.sys.stdin", SimpleNamespace(isatty=lambda: True)),
        patch("builtins.print") as printed,
        patch("builtins.input", return_value="да"),
    ):
        assert confirm_nas_storage("nas", forward, RuntimeError) == 0
    shown = "\n".join(str(call.args[0]) for call in printed.call_args_list)
    assert "SHARED-TEST" in shown
    assert "НЕ ФОРМАТИРУЕТСЯ" in shown
    assert "ZFS-A" in shown and "ZFS-B" in shown
    assert "/dev/sdc" in shown and "разделов: 0" in shown
    assert calls[-1][0] == ["deploy", "nas", "--_physical_stage", "prepare"]
    assert json.loads(calls[-1][1]) == {"fingerprint": plan.fingerprint}


def test_missing_disk_state_blocks_approval() -> None:
    """Нельзя подтверждать диск, если не выведено его текущее состояние."""

    _host, plan = sample_plan()
    output = {
        "fingerprint": plan.fingerprint,
        "requires_creation": True,
        "items": [
            {"role": item.role, "serial": item.serial, "action": item.action}
            for item in plan.assignments
        ],
    }
    calls = []

    def forward(command, *, capture_output=False, input_text=None):
        calls.append(command)
        if capture_output:
            return SimpleNamespace(returncode=0, stderr="",
                stdout="NAS_STORAGE_PLAN_JSON=" + json.dumps(output) + "\n")
        return 0

    with (
        patch("infra_manager_nas_storage.sys.stdin",
              SimpleNamespace(isatty=lambda: True)),
        patch("builtins.input", side_effect=AssertionError("Нельзя просить подтверждение")),
    ):
        refuses(lambda: confirm_nas_storage("nas", forward, RuntimeError), RuntimeError)
    assert ["deploy", "nas", "--_physical_stage", "prepare"] not in calls


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


def test_recovered_result_uses_original_approved_fingerprint() -> None:
    """После форматирования текущий preserve-план не заменяет исходный отпечаток."""

    host, original = sample_plan()
    original_fingerprint = original.fingerprint
    disks = (
        DiskInspection("/dev/sda", "SPCC", "SYSTEM-TEST",
                       120_000_000_000, True, True),
        DiskInspection("/dev/sdb", "ST1000DM003", "SHARED-TEST",
                       1_000_000_000_000, False, True),
        DiskInspection("/dev/sdc", "ST4000VX016", "ZFS-A",
                       4_000_000_000_000, False, True),
        DiskInspection("/dev/sdd", "ST4000VX016", "ZFS-B",
                       4_000_000_000_000, False, True),
    )
    current = propose_storage(host.devices, disks)
    assert all(item.action == "preserve" for item in current.assignments)
    assert original_fingerprint != current.fingerprint
    report = {
        "fingerprint": original_fingerprint,
        "serials": {item.role: item.serial for item in current.assignments
                    if item.role != "system"},
        "volumes": {
            "shared": {
                "mountpoint": "/srv/dev-disk-by-uuid-62efe809-f358-4c75-a465-63fc7c617104",
                "filesystem_uuid": "62efe809-f358-4c75-a465-63fc7c617104",
                "omv_reference": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            },
            "data": {
                "mountpoint": "/nas-data", "pool": "nas-data",
                "pool_guid": "4080170063689670128",
                "omv_reference": "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff",
            },
            "backup": {
                "mountpoint": "/nas-backup", "pool": "nas-backup",
                "pool_guid": "7608598756680166915",
                "omv_reference": "cccccccc-dddd-4eee-8fff-aaaaaaaaaaaa",
            },
        },
    }
    # Прежняя проверка ошибочно сравнивала journal.fingerprint с текущим preserve-планом.
    refuses(lambda: _check_result(host, current, report), InfraManagerError)
    finished = _check_result(
        host, current, report, expected_fingerprint=original_fingerprint
    )
    assert finished.volumes == report["volumes"]
    assert {item["serial"] for item in finished.devices} == {
        "SYSTEM-TEST", "SHARED-TEST", "ZFS-A", "ZFS-B"
    }
    # Ни чужой отпечаток, ни подмена HDD не проходят проверку восстановления.
    refuses(lambda: _check_result(
        host, current, {**report, "fingerprint": current.fingerprint},
        expected_fingerprint=original_fingerprint,
    ), InfraManagerError)
    refuses(lambda: _check_result(
        host, current, {**report, "serials": {**report["serials"], "data": "OTHER"}},
        expected_fingerprint=original_fingerprint,
    ), InfraManagerError)


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


def test_mixed_preflight_only_checks_blank_zfs_disks() -> None:
    """При подготовке смешанного набора shared не проходит через clean_disk."""

    script = _load_remote()
    items = [
        {"role": "shared", "serial": "SHARED-TEST", "action": "preserve"},
        {"role": "data", "serial": "ZFS-A", "action": "create-zfs"},
        {"role": "backup", "serial": "ZFS-B", "action": "create-zfs"},
    ]
    disks = [
        {"path": "/dev/sda", "type": "disk", "serial": "SYSTEM-TEST",
         "mountpoints": [None], "children": [
             {"type": "part", "mountpoints": ["/"], "children": []}]},
        {"path": "/dev/sdb", "type": "disk", "serial": "SHARED-TEST",
         "model": "ST1000DM003", "size": 1_000_000_000_000,
         "mountpoints": [None], "children": [
             {"type": "part", "fstype": "ext4", "mountpoints": ["/srv/shared"]}]},
        {"path": "/dev/sdc", "type": "disk", "serial": "ZFS-A",
         "model": "ST4000VX016", "size": 4_000_000_000_000, "mountpoints": [None]},
        {"path": "/dev/sdd", "type": "disk", "serial": "ZFS-B",
         "model": "ST4000VX016", "size": 4_000_000_000_000, "mountpoints": [None]},
    ]
    with (
        patch.object(script, "disk_list", return_value=disks),
        patch.object(script, "stable_device", side_effect=lambda _path, serial: "/dev/disk/by-id/" + serial),
        patch.object(script, "clean_disk") as clean,
        patch.object(script, "execute", return_value=SimpleNamespace(returncode=1, stdout="")),
    ):
        selected = script.preflight(items)
        assert set(selected) == {"shared", "data", "backup"}
        assert clean.call_count == 2
        cleaned = [call.args[0]["serial"] for call in clean.call_args_list]
        assert cleaned == ["ZFS-A", "ZFS-B"]
        disks[1]["children"] = []
        refuses(lambda: script.preflight(items), script.StorageError)


def test_preserved_shared_verification() -> None:
    """Не считать HDD 1 ТБ сохранённым без его UUID, OMV и реального mount."""

    script = _load_remote()
    uid = "12345678-1234-1234-1234-123456789abc"
    ref = "abcdefab-abcd-abcd-abcd-abcdefabcdef"
    path = "/srv/dev-disk-by-uuid-" + uid
    disk = {
        "serial": "SHARED-TEST", "path": "/dev/sdb", "type": "disk",
        "mountpoints": [None],
        "children": [{"path": "/dev/sdb1", "type": "part",
                      "fstype": "ext4", "uuid": uid}],
    }
    records = [{"uuid": ref, "dir": path, "fsname": "/dev/sdb1", "type": "ext4"}]
    mounted = {"filesystems": [
        {"target": path, "source": "/dev/sdb1", "fstype": "ext4", "uuid": uid},
    ]}

    def fake_execute(*args, **_kwargs):
        if args[0] == "blkid":
            return SimpleNamespace(returncode=0, stdout=uid + "\n")
        if args[0] == "findmnt":
            return SimpleNamespace(returncode=0, stdout=json.dumps(mounted))
        raise AssertionError("Существующий HDD проверяется только чтением")

    with (
        patch.object(script, "disk_list", return_value=[disk]),
        patch.object(script, "stable_device", return_value="/dev/disk/by-id/shared"),
        patch.object(script, "omv_records", return_value=records),
        patch.object(script, "execute", side_effect=fake_execute),
    ):
        assert script.inspect_preserved_shared("SHARED-TEST", "/dev/disk/by-id/shared") == {
            "filesystem_uuid": uid, "mountpoint": path, "omv_reference": ref,
        }
        mounted["filesystems"][0]["uuid"] = "wrong"
        refuses(lambda: script.inspect_preserved_shared(
            "SHARED-TEST", "/dev/disk/by-id/shared"
        ), script.StorageError)
        mounted["filesystems"][0]["uuid"] = uid
        records[0]["type"] = "ntfs"
        refuses(lambda: script.inspect_preserved_shared(
            "SHARED-TEST", "/dev/disk/by-id/shared"
        ), script.StorageError)


def test_apply_preserves_shared() -> None:
    """При смешанном плане не запускать mkfs, sgdisk или mount на HDD 1 ТБ."""

    script = _load_remote()
    items = [
        {"role": "shared", "serial": "SHARED-TEST", "action": "preserve"},
        {"role": "data", "serial": "ZFS-A", "action": "create-zfs"},
        {"role": "backup", "serial": "ZFS-B", "action": "create-zfs"},
    ]
    saved = {
        "filesystem_uuid": "12345678-1234-1234-1234-123456789abc",
        "mountpoint": "/srv/dev-disk-by-uuid-12345678-1234-1234-1234-123456789abc",
        "omv_reference": "abcdefab-abcd-abcd-abcd-abcdefabcdef",
    }
    commands = []
    registered = []

    def fake_execute(*args, **_kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=0, stdout="")

    def fake_register(role, *_args):
        registered.append(role)
        return "ffffffff-ffff-ffff-ffff-ffffffffffff"

    with tempfile.TemporaryDirectory() as directory:
        with (
            patch.object(script, "JOURNAL", Path(directory) / "storage-preparation.json"),
            patch.object(script, "preflight", return_value={
                "shared": "/dev/disk/by-id/shared",
                "data": "/dev/disk/by-id/data",
                "backup": "/dev/disk/by-id/backup",
            }),
            patch.object(script, "inspect_preserved_shared", return_value=saved) as inspect,
            patch.object(script, "save_journal"),
            patch.object(script, "execute", side_effect=fake_execute),
            patch.object(script, "register_omv", side_effect=fake_register),
            patch.object(script, "verify_mount"),
            patch.object(script, "pool_guid", return_value="12345"),
        ):
            result = script.apply(items, "f" * 64)
    inspect.assert_called_once_with("SHARED-TEST", "/dev/disk/by-id/shared")
    assert result["volumes"]["shared"] == saved
    assert registered == ["data", "backup"]
    assert sum(args[:2] == ("zpool", "create") for args in commands) == 2
    assert not any(args[0] in {"sgdisk", "mkfs.ext4", "mount"} for args in commands)


def test_omv_new_mount_is_created_by_omv() -> None:
    """OMV 8 присваивает UUID новой записи сам, а не принимает наш UUID."""

    script = _load_remote()
    u = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    entry = {"uuid": u, "fsname": "/dev/disk/by-uuid/" + u,
             "dir": "/srv/dev-disk-by-uuid-" + u, "type": "ext4"}
    commands = []
    calls = 0

    def read_records():
        nonlocal calls
        calls += 1
        return [] if calls == 1 else [entry]

    def update(*args):
        commands.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.object(script, "omv_records", side_effect=read_records),
        patch.object(script, "execute", side_effect=update),
    ):
        assigned = script.register_omv("shared", "ext4",
                                      entry["fsname"], entry["dir"])
    assert assigned == u
    assert commands[0][:3] == (
        "omv-confdbadm", "update", "conf.system.filesystem.mountpoint"
    )
    payload = json.loads(commands[0][3])
    assert "uuid" not in payload
    assert payload["fsname"] == entry["fsname"]
    # Для существующего однозначного тома операция должна быть только чтением.
    with (
        patch.object(script, "omv_records", return_value=[entry]),
        patch.object(script, "execute") as command,
    ):
        assert script.register_omv("shared", "ext4",
                                  entry["fsname"], entry["dir"]) == u
        command.assert_not_called()



def test_omv_new_zfs_mount_is_created_without_caller_uuid() -> None:
    """Новый ZFS-пул регистрируется через OMV и при повторе не дублируется."""

    script = _load_remote()
    assigned_uuid = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
    entry = {
        "uuid": assigned_uuid, "fsname": "nas-data",
        "dir": "/nas-data", "type": "zfs",
    }
    commands = []
    reads = 0

    def read_records():
        nonlocal reads
        reads += 1
        return [] if reads == 1 else [entry]

    def run_command(*argv):
        commands.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.object(script, "omv_records", side_effect=read_records),
        patch.object(script, "execute", side_effect=run_command),
    ):
        assert script.register_omv("data", "zfs", "nas-data", "/nas-data") == assigned_uuid
    assert len(commands) == 1
    payload = json.loads(commands[0][3])
    assert commands[0][:3] == (
        "omv-confdbadm", "update", "conf.system.filesystem.mountpoint"
    )
    assert "uuid" not in payload
    assert payload["fsname"] == "nas-data"
    assert payload["dir"] == "/nas-data"
    assert payload["type"] == "zfs"

    with (
        patch.object(script, "omv_records", return_value=[entry]),
        patch.object(script, "execute") as command,
    ):
        assert script.register_omv("data", "zfs", "nas-data", "/nas-data") == assigned_uuid
        command.assert_not_called()


def test_recover_existing_hdds_never_reformats() -> None:
    """Ранее созданные ext4 и ZFS проверяются, но не форматируются заново."""

    script = _load_remote()
    old_items = [
        {"role": "shared", "serial": "SHARED-TEST", "action": "create-ext4"},
        {"role": "data", "serial": "ZFS-A", "action": "create-zfs"},
        {"role": "backup", "serial": "ZFS-B", "action": "create-zfs"},
    ]
    disks = [
        {"type": "disk", "path": "/dev/sda", "serial": "SYSTEM-TEST",
         "children": [{"mountpoints": ["/"]}]},
        {"type": "disk", "path": "/dev/sdb", "serial": "SHARED-TEST",
         "model": "ST1000DM003", "size": 1000000000000, "pttype": "gpt",
         "children": [{"type": "part", "fstype": "ext4",
                       "uuid": "62efe809-f358-4c75-a465-63fc7c617104"}]},
        {"type": "disk", "path": "/dev/sdc", "serial": "ZFS-A",
         "model": "ST4000VX016", "size": 4000000000000, "pttype": "gpt",
         "children": [{"type": "part", "fstype": "zfs_member"},
                      {"type": "part", "fstype": None}]},
        {"type": "disk", "path": "/dev/sdd", "serial": "ZFS-B",
         "model": "ST4000VX016", "size": 4000000000000, "pttype": "gpt",
         "children": [{"type": "part", "fstype": "zfs_member"},
                      {"type": "part", "fstype": None}]},
    ]
    commands = []
    def fake_execute(*args, **kwargs):
        commands.append(args)
        if args[:3] == ("blkid", "-s", "UUID"):
            return SimpleNamespace(returncode=0, stdout="62efe809-f358-4c75-a465-63fc7c617104\n")
        if args[:3] == ("blkid", "-s", "LABEL"):
            return SimpleNamespace(returncode=0, stdout="nas-shared\n")
        if args[0] == "zpool" and args[1] == "status":
            role = args[-1].removeprefix("nas-")
            serial = "ZFS-A" if role == "data" else "ZFS-B"
            return SimpleNamespace(returncode=0,
                stdout=f"  pool: {args[-1]}\n state: ONLINE\n"
                       f"    /dev/disk/by-id/ata_{serial}-part1 ONLINE 0 0 0\n")
        if args[0] == "findmnt" and args[1] == "--mountpoint":
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout="")

    with tempfile.TemporaryDirectory() as folder:
        journal = Path(folder) / "storage-preparation.json"
        journal.write_text(json.dumps({
            "fingerprint": "f" * 64, "state": "started", "items": old_items,
        }))
        for serial in ("SHARED-TEST", "ZFS-A", "ZFS-B"):
            Path(folder, serial + "-part1").touch()

        with (
            patch.object(script, "JOURNAL", journal),
            patch.object(script, "disk_list", return_value=disks),
            patch.object(script, "stable_device",
                         side_effect=lambda _path, serial: str(Path(folder, serial))),
            patch.object(script, "execute", side_effect=fake_execute),
            patch.object(script, "register_omv",
                         return_value="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
            patch.object(script, "pool_guid", return_value="12345"),
            patch.object(script, "verify_mount") as verify,
        ):
            result = script.recover_from_journal(old_items, "f" * 64)
            assert result["volumes"]["shared"]["filesystem_uuid"] == (
                "62efe809-f358-4c75-a465-63fc7c617104"
            )
            assert len(result["volumes"]) == 3
            assert (Path(folder) / "storage-preparation-result.json").exists()
            assert journal.exists()
            assert not any(args[0] in {
                "sgdisk", "mkfs.ext4", "wipefs", "zpool-create"
            } for args in commands)
            assert not any(args[:2] == ("zpool", "create") for args in commands)
            verify.assert_any_call("/nas-data", "zfs", "nas-data")
            verify.assert_any_call("/nas-backup", "zfs", "nas-backup")
            # Отчёт/исходный журнал остаются на месте; повтор безопасен.
            script.recover_from_journal(old_items, "f" * 64)
            wrong = [dict(item) for item in old_items]
            wrong[-1]["serial"] = "ANOTHER"
            refuses(lambda: script.recover_from_journal(
                wrong, "f" * 64), script.StorageError)
            assert not any(args[:2] == ("zpool", "create") for args in commands)


def test_pending_recovery_is_auto_dispatched_without_format_prompt() -> None:
    """После исходного подтверждения достаточно нового deploy nas без записи."""

    host, first_plan = sample_plan()
    all_existing = (
        DiskInspection("/dev/sda", "SPCC", "SYSTEM-TEST", 120_000_000_000, True, True),
        DiskInspection("/dev/sdb", "ST1000DM003", "SHARED-TEST", 1_000_000_000_000, False, True),
        DiskInspection("/dev/sdc", "ST4000VX016", "ZFS-A", 4_000_000_000_000, False, True),
        DiskInspection("/dev/sdd", "ST4000VX016", "ZFS-B", 4_000_000_000_000, False, True),
    )
    current_plan = propose_storage(host.devices, all_existing)
    journal = {
        "fingerprint": first_plan.fingerprint, "state": "started",
        "items": [
            {"role": item.role, "serial": item.serial, "action": item.action}
            for item in first_plan.assignments if item.role != "system"
        ],
    }
    from infra_manager.physical_nas_storage_prepare import read_pending_recovery
    mock_session = SimpleNamespace(command=lambda text: ("ssh", text))
    with patch(
        "infra_manager.physical_nas_storage_prepare.run",
        return_value=SimpleNamespace(returncode=0, stdout=json.dumps(journal)),
    ):
        assert read_pending_recovery(host, mock_session, current_plan) == journal
    completed_host = replace(host, volumes={
        "shared": {"filesystem_uuid": "62efe809-f358-4c75-a465-63fc7c617104"},
        "data": {"pool_guid": "123456789"},
        "backup": {"pool_guid": "987654321"},
    })
    with patch("infra_manager.physical_nas_storage_prepare.run") as remote:
        assert read_pending_recovery(completed_host, mock_session, current_plan) is None
        remote.assert_not_called()
    wrong = dict(journal)
    wrong["fingerprint"] = "0" * 64
    with patch(
        "infra_manager.physical_nas_storage_prepare.run",
        return_value=SimpleNamespace(returncode=0, stdout=json.dumps(wrong)),
    ):
        refuses(lambda: read_pending_recovery(host, mock_session, current_plan),
                InfraManagerError)

    response = {
        "fingerprint": current_plan.fingerprint, "requires_creation": False,
        "recovery_fingerprint": first_plan.fingerprint,
        "items": [
            {"role": a.role, "serial": a.serial, "action": a.action,
             "current_state": observed_disk_condition(a)}
            for a in current_plan.assignments
        ],
    }
    calls = []
    def forward(cmd, *, capture_output=False, input_text=None):
        calls.append((cmd, input_text))
        if capture_output:
            return SimpleNamespace(
                returncode=0, stdout="NAS_STORAGE_PLAN_JSON=" + json.dumps(response),
            )
        return 0

    with (
        patch("infra_manager_nas_storage.sys.stdin",
              SimpleNamespace(isatty=lambda: False)),
        patch("builtins.input", side_effect=AssertionError("Нельзя снова спрашивать о форматировании")),
    ):
        assert confirm_nas_storage("nas", forward, RuntimeError) == 0
    assert calls[-1][0] == ["deploy", "nas", "--_physical_stage", "prepare"]
    assert json.loads(calls[-1][1]) == {
        "fingerprint": first_plan.fingerprint
    }


def main():
    """Проверить все контракты без обращения к реальным HDD."""

    test_confirmation()
    test_existing_shared_confirmation()
    test_missing_disk_state_blocks_approval()
    test_storage_state()
    test_recovered_result_uses_original_approved_fingerprint()
    test_remote_preflight()
    test_mixed_preflight_only_checks_blank_zfs_disks()
    test_preserved_shared_verification()
    test_apply_preserves_shared()
    test_omv_new_mount_is_created_by_omv()
    test_omv_new_zfs_mount_is_created_without_caller_uuid()
    test_recover_existing_hdds_never_reformats()
    test_pending_recovery_is_auto_dispatched_without_format_prompt()
    print("[ОК] Подтверждение и подготовка HDD проверены на искусственных данных")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

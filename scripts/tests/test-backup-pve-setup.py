#!/usr/bin/env python3
"""Проверки безопасного интерфейса резервирования без настоящего PVE/NAS."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
BACKUP = ROOT / "scripts/backup"


def load(filename: str, label: str):
    spec = importlib.util.spec_from_file_location(label, BACKUP / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


core = load("backup-pve.py", "test_pve_core")
device = load("backup-device.py", "test_backup_device")
mount = load("backup-mount.py", "test_backup_mount")
setup = load("backup-setup.py", "test_backup_setup")
restore = load("backup-restore.py", "test_backup_restore")


def refusal(action, exception):
    try:
        action()
    except exception:
        return
    raise AssertionError("Небезопасная операция должна была быть запрещена")


def test_blank_device_requires_exact_confirmation():
    with tempfile.TemporaryDirectory() as root:
        incorrect = Path(root) / "disk"
        refusal(
            lambda: device.empty_disk(incorrect, "REAL-SERIAL"),
            device.DeviceError,
        )
    with (
        patch.object(device, "empty_disk", return_value=("/dev/sdx", 240_000_000_000)),
        patch.object(device, "confirmation", return_value="ФОРМАТИРОВАТЬ-TEST"),
        patch.object(device, "execute") as execute,
    ):
        refusal(
            lambda: device.prepare(Path("/dev/disk/by-id/test"), "SERIAL", "other"),
            device.DeviceError,
        )
        execute.assert_not_called()


def test_fstab_does_not_replace_existing_mount():
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "fstab"
        path.write_text("# base\n", encoding="utf-8")
        point = Path("/mnt/pve/backup-fast")
        rule = "UUID=test /mnt/pve/backup-fast ext4 defaults 0 2"
        with patch.object(mount, "FSTAB", path):
            mount.add_fstab(rule, point)
            mount.add_fstab(rule, point)
            assert path.read_text(encoding="utf-8").count(rule) == 1
            refusal(
                lambda: mount.add_fstab(
                    "UUID=another /mnt/pve/backup-fast ext4 defaults 0 2", point,
                ),
                mount.MountError,
            )


def test_password_created_once_and_private():
    with tempfile.TemporaryDirectory() as root:
        path = Path(root) / "secret"
        setup.initial_password(path)
        original = path.read_bytes()
        assert len(original) > 48
        assert path.stat().st_mode & 0o077 == 0
        setup.initial_password(path)
        assert path.read_bytes() == original


def test_restore_never_writes_live():
    with tempfile.TemporaryDirectory() as root:
        target = Path(root) / "restore"
        target.mkdir(mode=0o700)
        restore.check_target(target)
        (target / "conflict").write_text("data", encoding="utf-8")
        refusal(lambda: restore.check_target(target), restore.core.BackupError)
    refusal(lambda: restore.check_target(Path("/etc")), restore.core.BackupError)


def test_vm_manifest_rejects_unexpected_vm():
    with patch.object(core.shutil, "which", return_value="/usr/bin/vzdump"):
        refusal(
            lambda: core.backup_vm100(
                {"guests": [{"vmid": 910}]},
                [Path("/mnt/ssd"), Path("/mnt/nas")], Path("/run/secret"),
            ),
            core.BackupError,
        )


if __name__ == "__main__":
    test_blank_device_requires_exact_confirmation()
    test_fstab_does_not_replace_existing_mount()
    test_password_created_once_and_private()
    test_restore_never_writes_live()
    test_vm_manifest_rejects_unexpected_vm()
    print("[ОК] Разметка SSD, ключи и пробное восстановление защищены")

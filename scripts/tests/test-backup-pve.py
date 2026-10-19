#!/usr/bin/env python3
"""Безопасность общего копирования PVE: без настоящих дисков, OpenBao и restic."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "scripts/backup/backup-pve.py"
spec = importlib.util.spec_from_file_location("pve_backup_test", MODULE)
assert spec and spec.loader
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def refuses(action, expected: str) -> None:
    try:
        action()
    except backup.BackupError as exc:
        assert expected in str(exc), str(exc)
    else:
        raise AssertionError(f"Операция должна была отказаться: {expected}")


def test_unconfirmed_targets() -> None:
    config = backup.load_config(ROOT / "infrastructure/hosts/pve/backup.yaml")
    assert config["host"] == "pve"
    assert len(config["destinations"]) == 2
    # Неразмеченный, неизвестный или не подключённый SSD не получает запись.
    refuses(lambda: backup.check_destinations(config["destinations"]), "пока не подтверждён")


def test_wrong_device_and_same_device() -> None:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        p1, p2 = root / "ssd", root / "nas"
        for point in (p1, p2):
            (point / "infra-core").mkdir(parents=True)
            (point / "infra-core/config").write_text("ready", encoding="utf-8")
        targets = [
            dict(id="fast-ssd", mountpoint=str(p1), filesystem="ext4",
                 uuid="UUID1", source=None, repository="infra-core"),
            dict(id="nas-backup", mountpoint=str(p2), filesystem="zfs",
                 uuid=None, source="nas-backup/pve", repository="infra-core"),
        ]
        def mounted(where):
            if where == p1:
                return dict(target=str(p1), source="/dev/sdb1", fstype="ext4", uuid="UUID1")
            return dict(target=str(p2), source="nas-backup/pve", fstype="zfs", uuid="")
        with patch.object(backup, "_mounted_device", side_effect=mounted):
            assert backup.check_destinations(targets) == [p1 / "infra-core", p2 / "infra-core"]
            wrong = [dict(entry) for entry in targets]
            wrong[0]["uuid"] = "OTHER"
            refuses(lambda: backup.check_destinations(wrong), "UUID")
            wrong = [dict(entry) for entry in targets]
            wrong[1]["source"] = "another-dataset"
            refuses(lambda: backup.check_destinations(wrong), "Источник")
            wrong = [dict(entry) for entry in targets]
            wrong[1]["filesystem"] = "ntfs"
            refuses(lambda: backup.check_destinations(wrong), "только ext4 или zfs")
            wrong = [dict(entry) for entry in targets]
            wrong[1]["source"] = "/dev/sdb1"
            # Оба пути указывают на один и тот же реальный источник;
            # второй файловой системой остаётся zfs.
            def same_device(where):
                item = mounted(where)
                if where == p2:
                    item["source"] = "/dev/sdb1"
                return item

            with patch.object(backup, "_mounted_device", side_effect=same_device):
                refuses(lambda: backup.check_destinations(wrong), "одном источнике")


def test_password_mode() -> None:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "password"
        path.write_text("secret", encoding="utf-8")
        path.chmod(0o644)
        refuses(lambda: backup._regular_file(path, secret=True), "широкие")
        path.chmod(0o600)
        backup._regular_file(path, secret=True)


def test_plan_does_not_take_snapshot() -> None:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "password"
        path.write_text("private", encoding="utf-8")
        path.chmod(0o600)
        first, second = Path(folder) / "ssd", Path(folder) / "nas"
        config = {
            "password_file": str(path),
            "destinations": [{}, {}],
            "openbao": {},
        }
        calls = []
        with (
            patch.object(backup, "check_source"),
            patch.object(backup, "check_destinations", return_value=[first, second]),
            patch.object(backup, "check_openbao_credentials"),
            patch.object(backup.shutil, "which", return_value="/usr/bin/restic"),
            patch.object(backup, "restic_command", side_effect=lambda *a: calls.append(a)),
            patch.object(backup, "sqlite_snapshot") as sqlite_copy,
            patch.object(backup, "openbao_snapshot") as raft_copy,
        ):
            backup.backup(config)
        assert len(calls) == 2
        assert all(call[-1] == ["snapshots", "--json"] for call in calls)
        sqlite_copy.assert_not_called()
        raft_copy.assert_not_called()


def test_no_partial_success_if_snapshot_fails() -> None:
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        password = root / "password"
        password.write_text("test-only", encoding="utf-8")
        password.chmod(0o600)
        targets = [root / "ssd", root / "nas"]
        config = {
            "password_file": str(password),
            "destinations": [{}, {}],
            "openbao": {},
        }
        calls = []
        original_temporary = tempfile.TemporaryDirectory
        with (
            patch.object(backup, "check_source"),
            patch.object(backup, "check_destinations", return_value=targets),
            patch.object(backup, "check_openbao_credentials"),
            patch.object(backup.shutil, "which", return_value="/usr/bin/restic"),
            patch.object(backup, "restic_command", side_effect=lambda *a: calls.append(a)),
            patch.object(backup, "operation_guard", return_value=contextlib.nullcontext()),
            patch.object(backup, "sqlite_snapshot"),
            patch.object(backup, "openbao_snapshot", side_effect=backup.BackupError("Raft недоступен")),
            patch.object(backup.tempfile, "TemporaryDirectory", side_effect=lambda **k: original_temporary(dir=root)),
        ):
            refuses(lambda: backup.backup(config, execute=True), "Raft недоступен")
        assert len(calls) == 2, "Нельзя записать неполный комплект при ошибке Raft"


def test_config_safe() -> None:
    with tempfile.TemporaryDirectory() as folder:
        f = Path(folder) / "unsafe.yaml"
        f.write_text("schema_version: 1\nhost: other\njob: infra-core\ndestinations: []\n", encoding="utf-8")
        refuses(lambda: backup.load_config(f), "этого PVE")
        f.unlink()
        f.symlink_to("/etc/hosts")
        refuses(lambda: backup.load_config(f), "описания")


if __name__ == "__main__":
    test_unconfirmed_targets()
    test_wrong_device_and_same_device()
    test_password_mode()
    test_plan_does_not_take_snapshot()
    test_no_partial_success_if_snapshot_fails()
    test_config_safe()
    print("[ОК] Резервирование PVE отказывает при непроверенном носителе и неполной копии")

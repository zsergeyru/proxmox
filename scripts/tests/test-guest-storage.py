#!/usr/bin/env python3
"""Проверки двух постоянных томов без обращения к настоящему PVE."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
HOST_SCRIPT = ROOT / "scripts/infra-manager/host/guest-storage.py"
spec = importlib.util.spec_from_file_location("guest_storage_host", HOST_SCRIPT)
assert spec is not None and spec.loader is not None
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)

sys.path.insert(0, str(ROOT / "scripts/infra-manager"))
from infra_manager import pve_host


def storage_config() -> dict:
    return {
        "volume_group": "pve",
        "thin_pool": "data",
        "volumes": [
            {
                "name": "photo_archive",
                "size_gb": 240,
                "mountpoint": "/mnt/bindmounts/photo-archive",
            },
            {
                "name": "infra_state",
                "size_gb": 40,
                "mountpoint": "/mnt/bindmounts/infra-state",
            },
        ],
        "mounts": [
            {
                "slot": "mp0",
                "volume": "photo_archive",
                "directory": "data",
                "target": "/srv/photo-archive",
                "read_only": True,
            },
            {
                "slot": "mp1",
                "volume": "infra_state",
                "directory": "340-photo-archive",
                "target": "/mnt/persistent-state",
                "read_only": False,
            },
            {
                "slot": "mp2",
                "volume": "photo_archive",
                "directory": "immich-media",
                "target": "/srv/photo-archive-media",
                "read_only": False,
            },
        ],
    }


def test_contract() -> None:
    vg, pool, volumes, mounts = storage.validate(340, storage_config())
    assert (vg, pool) == ("pve", "data")
    assert set(volumes) == {"photo_archive", "infra_state"}
    assert volumes["photo_archive"]["size_gb"] == 240
    assert volumes["infra_state"]["size_gb"] == 40
    assert [str(storage.source_path(volumes, item)) for item in mounts] == [
        "/mnt/bindmounts/photo-archive/data",
        "/mnt/bindmounts/infra-state/340-photo-archive",
        "/mnt/bindmounts/photo-archive/immich-media",
    ]
    bad = storage_config()
    bad["mounts"][1]["volume"] = "photo_archive"
    bad["mounts"][1]["directory"] = "data"
    try:
        storage.validate(340, bad)
    except storage.StorageError:
        pass
    else:
        raise AssertionError("Повторный каталог должен быть запрещён")
    bad = storage_config()
    bad["volumes"][1]["mountpoint"] = "/mnt/bindmounts/photo-archive"
    try:
        storage.validate(340, bad)
    except storage.StorageError:
        pass
    else:
        raise AssertionError("Повторная точка монтирования должна быть запрещена")
    bad = storage_config()
    bad["mounts"][0]["volume"] = "missing"
    try:
        storage.validate(340, bad)
    except storage.StorageError:
        pass
    else:
        raise AssertionError("Ссылка на неизвестный том должна быть запрещена")


def test_existing_mounts() -> None:
    config = (
        "hostname: photo-archive\nunprivileged: 1\n"
        "mp0: /mnt/bindmounts/photo-archive/data,mp=/srv/photo-archive,ro=1\n"
    )
    cfg = storage_config()
    volumes = {v["name"]: v for v in cfg["volumes"]}
    with patch.object(storage, "command", return_value=SimpleNamespace(stdout=config)):
        missing = storage.existing_mount_config(340, volumes, cfg["mounts"])
    assert [item["slot"] for item in missing] == ["mp1", "mp2"]

    conflicted = config.replace("ro=1", "ro=0")
    with patch.object(storage, "command", return_value=SimpleNamespace(stdout=conflicted)):
        try:
            storage.existing_mount_config(340, volumes, cfg["mounts"])
        except storage.StorageError:
            pass
        else:
            raise AssertionError("Чужой mp0 нельзя перезаписывать")


def test_existing_volume_not_formatted() -> None:
    calls: list[tuple] = []

    def fake_command(*args, check=True):
        calls.append(args)
        if args[:4] == ("lvs", "--noheadings", "-o", "lv_name"):
            return SimpleNamespace(returncode=0, stdout="data photo_archive infra_state")
        if args[0] == "lvs" and "lv_size" in args:
            return SimpleNamespace(returncode=0, stdout=str(240 * 1024**3))
        if args[0] == "lvs" and "pool_lv" in args:
            return SimpleNamespace(returncode=0, stdout="data")
        if args[:4] == ("blkid", "-s", "TYPE", "-o"):
            return SimpleNamespace(returncode=0, stdout="ext4")
        if args[:4] == ("blkid", "-s", "UUID", "-o"):
            return SimpleNamespace(returncode=0, stdout="abc123-456")
        raise AssertionError(f"Непредвиденная команда: {args}")

    photo = storage_config()["volumes"][0]
    with patch.object(storage, "command", side_effect=fake_command):
        device, uuid = storage.ensure_volume("pve", "data", photo)
    assert str(device) == "/dev/pve/photo_archive"
    assert uuid == "abc123-456"
    assert not any(args[0] in ("mkfs.ext4", "lvcreate") for args in calls)

    # Повторный deploy не откатывает вручную увеличенный том.
    def larger_command(*args, check=True):
        if args[:4] == ("lvs", "--noheadings", "-o", "lv_name"):
            return SimpleNamespace(returncode=0, stdout="data photo_archive")
        if args[0] == "lvs" and "lv_size" in args:
            return SimpleNamespace(returncode=0, stdout=str(300 * 1024**3))
        if args[0] == "lvs" and "pool_lv" in args:
            return SimpleNamespace(returncode=0, stdout="data")
        if args[:4] == ("blkid", "-s", "TYPE", "-o"):
            return SimpleNamespace(returncode=0, stdout="ext4")
        if args[:4] == ("blkid", "-s", "UUID", "-o"):
            return SimpleNamespace(returncode=0, stdout="abc123-456")
        raise AssertionError(f"Непредвиденная команда: {args}")

    with patch.object(storage, "command", side_effect=larger_command):
        storage.ensure_volume("pve", "data", photo)


def test_new_volume_only_formatted_once() -> None:
    calls: list[tuple] = []

    def fake_command(*args, check=True):
        calls.append(args)
        if args[0] == "lvs":
            return SimpleNamespace(returncode=0, stdout="data vm-340-disk-0")
        if args[0] in ("lvcreate", "mkfs.ext4"):
            return SimpleNamespace(returncode=0, stdout="")
        if args[:4] == ("blkid", "-s", "TYPE", "-o"):
            count = sum(call[0] == "mkfs.ext4" for call in calls)
            return SimpleNamespace(
                returncode=0 if count else 2,
                stdout="ext4" if count else "",
            )
        if args[:4] == ("blkid", "-s", "UUID", "-o"):
            return SimpleNamespace(returncode=0, stdout="abc123-456")
        raise AssertionError(f"Непредвиденная команда: {args}")

    with patch.object(storage, "command", side_effect=fake_command):
        storage.ensure_volume("pve", "data", storage_config()["volumes"][1])
    assert [args[0] for args in calls].count("lvcreate") == 1
    assert [args[0] for args in calls].count("mkfs.ext4") == 1


def test_unknown_existing_volume_does_not_format() -> None:
    calls: list[tuple] = []

    def fake_command(*args, check=True):
        calls.append(args)
        if args[:4] == ("lvs", "--noheadings", "-o", "lv_name"):
            return SimpleNamespace(returncode=0, stdout="data infra_state")
        if args[0] == "lvs" and "lv_size" in args:
            return SimpleNamespace(returncode=0, stdout=str(40 * 1024**3))
        if args[0] == "lvs" and "pool_lv" in args:
            return SimpleNamespace(returncode=0, stdout="data")
        if args[0] == "blkid":
            return SimpleNamespace(returncode=2, stdout="")
        raise AssertionError(f"Непредвиденная команда: {args}")

    with patch.object(storage, "command", side_effect=fake_command):
        try:
            storage.ensure_volume("pve", "data", storage_config()["volumes"][1])
        except storage.StorageError:
            pass
        else:
            raise AssertionError("Неизвестный существующий том нельзя форматировать")
    assert not any(args[0] in ("mkfs.ext4", "lvcreate") for args in calls)


def test_guest_mount_paths() -> None:
    config = storage_config()
    volumes = {v["name"]: v for v in config["volumes"]}
    calls: list[tuple] = []

    def fake_command(*args, check=True):
        calls.append(args)
        if args[:2] == ("pct", "status"):
            return SimpleNamespace(returncode=0, stdout="status: stopped")
        return SimpleNamespace(returncode=0, stdout="")

    with patch.object(storage, "command", side_effect=fake_command):
        storage.configure_lxc(340, volumes, config["mounts"], config["mounts"])
    assert (
        "pct", "set", "340", "--mp0",
        "/mnt/bindmounts/photo-archive/data,mp=/srv/photo-archive,ro=1",
    ) in calls
    assert (
        "pct", "set", "340", "--mp1",
        "/mnt/bindmounts/infra-state/340-photo-archive,mp=/mnt/persistent-state,ro=0",
    ) in calls
    assert (
        "pct", "set", "340", "--mp2",
        "/mnt/bindmounts/photo-archive/immich-media,mp=/srv/photo-archive-media,ro=0",
    ) in calls
    assert not any(item[:2] == ("pct", "stop") for item in calls)


def test_no_storage_on_other_guests() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        guest = Path(tmp) / "infrastructure/guests/420-ui"
        guest.mkdir(parents=True)
        (guest / "provision.yaml").write_text("schema_version: 1\n", encoding="utf-8")
        with patch.object(pve_host, "_install_remote_file") as install:
            pve_host.ensure_guest_storage("pve", 420, "lxc", guest)
        install.assert_not_called()


def test_guest_storage_sent_to_pve() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        guest = Path(tmp) / "infrastructure/guests/340-photo-archive"
        guest.mkdir(parents=True)
        (guest / "provision.yaml").write_text(
            "pve_storage: " + json.dumps(storage_config()) + "\n",
            encoding="utf-8",
        )
        remote = []
        with (
            patch.object(pve_host, "_install_remote_file") as install,
            patch.object(
                pve_host, "_ssh_with_input",
                side_effect=lambda *args, **kwargs: remote.append((args, kwargs)),
            ),
        ):
            pve_host.ensure_guest_storage("pve", 340, "lxc", guest)
        install.assert_called_once()
        assert remote[0][0][:3] == ("pve", str(pve_host.GUEST_STORAGE_HOST_COMMAND), "340")
        assert json.loads(remote[0][1]["input_text"]) == storage_config()


if __name__ == "__main__":
    test_contract()
    test_existing_mounts()
    test_existing_volume_not_formatted()
    test_new_volume_only_formatted_once()
    test_unknown_existing_volume_does_not_format()
    test_guest_mount_paths()
    test_no_storage_on_other_guests()
    test_guest_storage_sent_to_pve()
    print("OK: Guest storage contract")

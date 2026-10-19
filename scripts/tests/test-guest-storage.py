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
                "read_only": False,
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
    """Разрешить только смену ro при том же источнике и точке подключения."""
    config = (
        "hostname: photo-archive\nunprivileged: 1\n"
        "mp0: /mnt/bindmounts/photo-archive/data,mp=/srv/photo-archive,ro=0\n"
    )
    cfg = storage_config()
    volumes = {v["name"]: v for v in cfg["volumes"]}
    with patch.object(storage, "command", return_value=SimpleNamespace(stdout=config)):
        pending = storage.existing_mount_config(340, volumes, cfg["mounts"])
    assert [item["slot"] for item in pending] == ["mp1", "mp2"]

    # Старый mp0 подключён только на чтение; его разрешено изменить на запись.
    old_read_only = config.replace("ro=0", "ro=1")
    with patch.object(storage, "command", return_value=SimpleNamespace(stdout=old_read_only)):
        pending = storage.existing_mount_config(340, volumes, cfg["mounts"])
    assert [item["slot"] for item in pending] == ["mp0", "mp1", "mp2"]

    # Неправильный каталог или посторонние настройки подключения не заменять.
    for invalid in (
        old_read_only.replace("photo-archive/data", "photo-archive/unknown"),
        old_read_only.replace("ro=1", "ro=1,backup=1"),
    ):
        with patch.object(storage, "command", return_value=SimpleNamespace(stdout=invalid)):
            try:
                storage.existing_mount_config(340, volumes, cfg["mounts"])
            except storage.StorageError:
                pass
            else:
                raise AssertionError("Чужое или нестандартное mp0 нельзя перезаписывать")



def test_existing_volume_not_formatted() -> None:
    calls = []

    def fake(*args, check=True):
        calls.append(args)
        if args[:4] == ("lvs", "--noheadings", "-o", "lv_name"):
            return SimpleNamespace(returncode=0, stdout="data photo_archive infra_state")
        if "lv_size" in args:
            return SimpleNamespace(returncode=0, stdout=str(300 * 1024**3))
        if "pool_lv" in args:
            return SimpleNamespace(returncode=0, stdout="data")
        if "lv_tags" in args:
            return SimpleNamespace(returncode=0, stdout="")
        if args[0] == "blkid" and "TYPE" in args:
            return SimpleNamespace(returncode=0, stdout="ext4")
        if args[0] == "blkid" and "UUID" in args:
            return SimpleNamespace(returncode=0, stdout="abc123-456")
        raise AssertionError(args)

    with patch.object(storage, "command", side_effect=fake):
        device, uuid = storage.ensure_volume("pve", "data", storage_config()["volumes"][0])
    assert (str(device), uuid) == ("/dev/pve/photo_archive", "abc123-456")
    assert not any(args[0] in ("lvcreate", "mkfs.ext4", "e2fsck") for args in calls)


def test_new_volume_and_interrupted_recovery() -> None:
    calls = []
    exists = False
    tags = ""
    formatted = False

    def fake(*args, check=True):
        nonlocal exists, tags, formatted
        calls.append(args)
        if args[:4] == ("lvs", "--noheadings", "-o", "lv_name"):
            return SimpleNamespace(returncode=0, stdout="data infra_state" if exists else "data")
        if args[0] == "lvcreate":
            exists = True
            tags = storage.PENDING_TAG
            return SimpleNamespace(returncode=0, stdout="")
        if "lv_size" in args:
            return SimpleNamespace(returncode=0, stdout=str(40 * 1024**3))
        if "pool_lv" in args:
            return SimpleNamespace(returncode=0, stdout="data")
        if "lv_tags" in args:
            return SimpleNamespace(returncode=0, stdout=tags)
        if args[0] == "mkfs.ext4":
            formatted = True
            return SimpleNamespace(returncode=0, stdout="")
        if args[0] == "e2fsck":
            return SimpleNamespace(returncode=0, stdout="")
        if args[0] == "lvchange":
            tags = ""
            return SimpleNamespace(returncode=0, stdout="")
        if args[0] == "blkid" and "TYPE" in args:
            return SimpleNamespace(
                returncode=0 if formatted else 2, stdout="ext4" if formatted else ""
            )
        if args[0] == "blkid" and "UUID" in args:
            return SimpleNamespace(returncode=0, stdout="abc123-456")
        raise AssertionError(args)

    volume = storage_config()["volumes"][1]
    with patch.object(storage, "command", side_effect=fake):
        storage.ensure_volume("pve", "data", volume)
        storage.ensure_volume("pve", "data", volume)
    assert [a[0] for a in calls].count("lvcreate") == 1
    assert [a[0] for a in calls].count("mkfs.ext4") == 1
    assert [a[0] for a in calls].count("e2fsck") == 1

    # LV с незавершённой меткой разрешается оформить после сбоя.
    exists, tags, formatted = True, storage.PENDING_TAG, False
    calls.clear()
    with patch.object(storage, "command", side_effect=fake):
        storage.ensure_volume("pve", "data", volume)
    assert [a[0] for a in calls].count("mkfs.ext4") == 1


def test_unknown_existing_volume_does_not_format() -> None:
    calls = []

    def fake(*args, check=True):
        calls.append(args)
        if args[:4] == ("lvs", "--noheadings", "-o", "lv_name"):
            return SimpleNamespace(returncode=0, stdout="data infra_state")
        if "lv_size" in args:
            return SimpleNamespace(returncode=0, stdout=str(40 * 1024**3))
        if "pool_lv" in args:
            return SimpleNamespace(returncode=0, stdout="data")
        if "lv_tags" in args:
            return SimpleNamespace(returncode=0, stdout="")
        if args[0] == "blkid":
            return SimpleNamespace(returncode=2, stdout="")
        raise AssertionError(args)

    with patch.object(storage, "command", side_effect=fake):
        try:
            storage.ensure_volume("pve", "data", storage_config()["volumes"][1])
        except storage.StorageError:
            pass
        else:
            raise AssertionError("Неизвестный том нельзя форматировать")
    assert not any(args[0] in ("mkfs.ext4", "lvcreate") for args in calls)


def test_originals_acl() -> None:
    """Разрешить изменение файлов, не трогая сами фотографии."""
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "data"
        photos = path / "originals"
        nested = photos / "2026" / "10"
        nested.mkdir(parents=True)
        original = nested / "image.jpg"
        original.write_bytes(b"existing photo")
        original_mode = original.stat().st_mode
        calls: list[tuple[str, ...]] = []
        volumes = {"photo_archive": {"mountpoint": temp}}
        mount = {
            "slot": "mp0", "volume": "photo_archive",
            "directory": "data", "target": "/srv/photo-archive", "read_only": False,
        }
        # source_path = temp/data; в тесте подменяем только изменение хозяина
        # и исполнение setfacl, сама файловая система остаётся реальной.
        with (
            patch.object(storage.os, "chown"),
            patch.object(storage.shutil, "which", return_value="/usr/bin/setfacl"),
            patch.object(
                storage, "command",
                side_effect=lambda *args, **kwargs: (
                    calls.append(args) or SimpleNamespace(returncode=0, stdout="")
                ),
            ),
        ):
            storage.ensure_directories(volumes, [mount])
            storage.ensure_directories(volumes, [mount])
        dirs = {str(photos), str(photos / "2026"), str(nested)}
        assert {
            args[-1] for args in calls if args[0] == "setfacl"
        } == dirs
        assert all(
            args[1:4] == ("-m", "u:100000:rwx,d:u:100000:rwx", args[-1])
            for args in calls
        )
        assert original.read_bytes() == b"existing photo"
        assert original.stat().st_mode == original_mode
        assert str(original) not in {args[-1] for args in calls}
        # При отсутствии пакета deploy устанавливает acl перед изменением
        # разрешений; повторная подготовка не запускает apt-get заново.
        installed = iter([False, True])
        def available(tool):
            if tool == "apt-get":
                return "/usr/bin/apt-get"
            if tool == "setfacl":
                return "/usr/bin/setfacl" if next(installed) else None
            raise AssertionError(tool)

        package_commands: list[tuple[str, ...]] = []
        with (
            patch.object(storage.os, "chown"),
            patch.object(storage.shutil, "which", side_effect=available),
            patch.object(
                storage, "command",
                side_effect=lambda *args, **kwargs: (
                    package_commands.append(args)
                    or SimpleNamespace(returncode=0, stdout="")
                ),
            ),
        ):
            storage.ensure_directories(volumes, [mount])
        assert package_commands[:2] == [
            ("apt-get", "update"),
            ("apt-get", "install", "-y", "--no-install-recommends", "acl"),
        ]
        assert [entry[0] for entry in package_commands[2:]] == ["setfacl"] * 3
        assert original.read_bytes() == b"existing photo"


def test_acl_install_failure_preserves_photos() -> None:
    """При недоступности apt развёртывание прекращается без изменения файлов."""
    with tempfile.TemporaryDirectory() as tmp:
        originals = Path(tmp) / "originals"
        originals.mkdir()
        photo = originals / "image.jpg"
        photo.write_bytes(b"original")
        def fake_command(*args, **_kwargs):
            assert args == ("apt-get", "update")
            raise storage.StorageError("apt-get update: источник пакетов недоступен")
        with (
            patch.object(
                storage.shutil, "which",
                side_effect=lambda tool: "/usr/bin/apt-get" if tool == "apt-get" else None,
            ),
            patch.object(storage, "command", side_effect=fake_command),
        ):
            try:
                storage.ensure_originals_writable(originals)
            except storage.StorageError as exc:
                assert "apt-get" in str(exc)
            else:
                raise AssertionError("Ошибка установки acl должна останавливать deploy")
        assert photo.read_bytes() == b"original"


def test_readonly_mount_preserves_originals() -> None:
    """В режиме просмотра ACL не обязателен и оригиналы не изменяются."""
    with tempfile.TemporaryDirectory() as temp:
        photos = Path(temp) / "data/originals"
        photos.mkdir(parents=True)
        (photos / "file.jpg").write_bytes(b"kept")
        volumes = {"photo_archive": {"mountpoint": temp}}
        with patch.object(storage.shutil, "which", side_effect=AssertionError("read-only")):
            storage.ensure_directories(
                volumes, [{
                    "slot": "mp0", "volume": "photo_archive",
                    "directory": "data", "target": "/srv/photo-archive",
                    "read_only": True,
                }],
            )
        assert (photos / "file.jpg").read_bytes() == b"kept"


def test_guest_mount_paths() -> None:
    config = storage_config()
    volumes = {v["name"]: v for v in config["volumes"]}
    calls = []

    def fake(*args, check=True):
        calls.append(args)
        if args[:2] == ("pct", "status"):
            return SimpleNamespace(returncode=0, stdout="status: stopped")
        return SimpleNamespace(returncode=0, stdout="")

    with (
        patch.object(storage, "command", side_effect=fake),
        patch.object(storage, "ensure_startup_hook") as hook,
    ):
        storage.configure_lxc(340, volumes, config["mounts"], config["mounts"], config)
    hook.assert_called_once_with(340, config)
    for number, path, target, readonly in [
        ("mp0", "photo-archive/data", "/srv/photo-archive", 0),
        ("mp1", "infra-state/340-photo-archive", "/mnt/persistent-state", 0),
        ("mp2", "photo-archive/immich-media", "/srv/photo-archive-media", 0),
    ]:
        assert (
            "pct", "set", "340", f"--{number}",
            f"/mnt/bindmounts/{path},mp={target},ro={readonly}",
        ) in calls


def test_startup_guard_blocks_missing_storage() -> None:
    config = storage_config()
    volumes = {v["name"]: v for v in config["volumes"]}
    with patch.object(storage, "command", return_value=SimpleNamespace(
        returncode=1, stdout=""
    )):
        try:
            storage.verify_mounted_volumes("pve", volumes, config["mounts"])
        except storage.StorageError:
            pass
        else:
            raise AssertionError("Нельзя запускать LXC без смонтированного тома")


def test_no_start_on_incomplete_setup() -> None:
    config = storage_config()
    volumes = {v["name"]: v for v in config["volumes"]}
    calls = []

    def fake(*args, check=True):
        calls.append(args)
        if args[:2] == ("pct", "status"):
            return SimpleNamespace(returncode=0, stdout="status: running")
        if args[:2] == ("pct", "set"):
            raise storage.StorageError("Имитация ошибки pct set")
        return SimpleNamespace(returncode=0, stdout="")

    with patch.object(storage, "command", side_effect=fake):
        try:
            storage.configure_lxc(340, volumes, config["mounts"], config["mounts"], config)
        except storage.StorageError:
            pass
        else:
            raise AssertionError("Ожидалась ошибка")
    assert any(args[:2] == ("pct", "shutdown") for args in calls)
    assert not any(args[:2] == ("pct", "start") for args in calls)



def test_ro_change_protected_guest() -> None:
    """Изменить только mp0 у защищённого активного LXC и перезапустить его."""
    cfg = storage_config()
    volumes = {v["name"]: v for v in cfg["volumes"]}
    current = (
        "unprivileged: 1\nprotection: 1\n"
        "hookscript: local:snippets/infra-manager-guest-storage.py\n"
        "mp0: /mnt/bindmounts/photo-archive/data,mp=/srv/photo-archive,ro=1\n"
        "mp1: /mnt/bindmounts/infra-state/340-photo-archive,"
        "mp=/mnt/persistent-state,ro=0\n"
        "mp2: /mnt/bindmounts/photo-archive/immich-media,"
        "mp=/srv/photo-archive-media,ro=0\n"
    )
    calls: list[tuple[str, ...]] = []
    running = True

    def fake(*args, check=True):
        nonlocal running
        calls.append(args)
        if args[:2] == ("pct", "config"):
            return SimpleNamespace(returncode=0, stdout=current)
        if args[:2] == ("pct", "shutdown"):
            running = False
        if args[:2] == ("pct", "start"):
            running = True
        if args[:2] == ("pct", "status"):
            return SimpleNamespace(
                returncode=0,
                stdout="status: running" if running else "status: stopped",
            )
        return SimpleNamespace(returncode=0, stdout="")

    with patch.object(storage, "command", side_effect=fake):
        pending = storage.existing_mount_config(340, volumes, cfg["mounts"])
        assert [item["slot"] for item in pending] == ["mp0"]
        with patch.object(storage, "ensure_startup_hook") as hook:
            storage.configure_lxc(340, volumes, cfg["mounts"], pending, cfg)
            hook.assert_called_once_with(340, cfg)
    shutdown = ("pct", "shutdown", "340", "--timeout", "60")
    off = ("pct", "set", "340", "--protection", "0")
    mp0 = (
        "pct", "set", "340", "--mp0",
        "/mnt/bindmounts/photo-archive/data,mp=/srv/photo-archive,ro=0",
    )
    on = ("pct", "set", "340", "--protection", "1")
    start = ("pct", "start", "340")
    assert calls.index(shutdown) < calls.index(off) < calls.index(mp0)
    assert calls.index(mp0) < calls.index(on) < calls.index(start)
    assert not any(a[3:4] in (("--mp1",), ("--mp2",)) for a in calls)


def test_protected_guest_keeps_protection_after_success() -> None:
    """Завершить настройку mp0/mp1/mp2 защищённого контейнера."""
    config = storage_config()
    volumes = {volume["name"]: volume for volume in config["volumes"]}
    calls: list[tuple[str, ...]] = []

    def fake(*args, check=True):
        calls.append(args)
        if args[:2] == ("pct", "status"):
            return SimpleNamespace(returncode=0, stdout="status: stopped")
        if args[:2] == ("pct", "config"):
            return SimpleNamespace(
                returncode=0,
                stdout="unprivileged: 1\nprotection: 1\n",
            )
        return SimpleNamespace(returncode=0, stdout="")

    with (
        patch.object(storage, "command", side_effect=fake),
        patch.object(storage, "ensure_startup_hook") as hook,
    ):
        storage.configure_lxc(340, volumes, config["mounts"], config["mounts"], config)
    hook.assert_called_once_with(340, config)
    off = calls.index(("pct", "set", "340", "--protection", "0"))
    on = calls.index(("pct", "set", "340", "--protection", "1"))
    assert off < on
    assert all(
        off < calls.index(("pct", "set", "340", f"--{item['slot']}",
            f"{storage.source_path(volumes, item)},mp={item['target']},ro={int(item['read_only'])}")) < on
        for item in config["mounts"]
    )
    start = calls.index(("pct", "start", "340"))
    assert on < start  # Защиту вернуть до включения LXC.


def test_protected_guest_restores_protection_after_error() -> None:
    """При ошибке mp1 защиту вернуть, контейнер не запускать."""
    config = storage_config()
    volumes = {volume["name"]: volume for volume in config["volumes"]}
    calls: list[tuple[str, ...]] = []

    def fake(*args, check=True):
        calls.append(args)
        if args[:2] == ("pct", "status"):
            return SimpleNamespace(returncode=0, stdout="status: running")
        if args[:2] == ("pct", "config"):
            return SimpleNamespace(returncode=0, stdout="protection: 1\n")
        if args[:3] == ("pct", "set", "340") and "--mp1" in args:
            raise storage.StorageError("Имитированная ошибка подключения")
        return SimpleNamespace(returncode=0, stdout="")

    with (
        patch.object(storage, "command", side_effect=fake),
        patch.object(storage, "ensure_startup_hook") as hook,
    ):
        try:
            storage.configure_lxc(340, volumes, config["mounts"], config["mounts"], config)
        except storage.StorageError:
            pass
        else:
            raise AssertionError("Ожидалась ошибка подключения")
    hook.assert_not_called()
    assert ("pct", "shutdown", "340", "--timeout", "60") in calls
    assert ("pct", "set", "340", "--protection", "0") in calls
    assert calls[-1] == ("pct", "set", "340", "--protection", "1")
    assert not any(item[:2] == ("pct", "start") for item in calls)


def test_existing_protected_mounts_do_not_toggle_protection() -> None:
    """Обычный повторный deploy защищённого 340 оставляет защиту в покое."""
    config = storage_config()
    volumes = {volume["name"]: volume for volume in config["volumes"]}
    calls: list[tuple[str, ...]] = []

    def fake(*args, check=True):
        calls.append(args)
        if args[:2] == ("pct", "status"):
            return SimpleNamespace(returncode=0, stdout="status: stopped")
        if args[:2] == ("pct", "config"):
            return SimpleNamespace(
                returncode=0,
                stdout=f"protection: 1\nhookscript: {storage.HOOK_ID}\n",
            )
        return SimpleNamespace(returncode=0, stdout="")

    with (
        patch.object(storage, "command", side_effect=fake),
        patch.object(storage, "ensure_startup_hook") as hook,
    ):
        storage.configure_lxc(340, volumes, config["mounts"], [], config)
    hook.assert_called_once_with(340, config)
    assert not any(item[:2] == ("pct", "set") for item in calls)
    # Даже при настроенных mp повторный deploy поднимет ранее остановленный LXC.
    assert ("pct", "start", "340") in calls


def test_protected_guest_hook_install_restores_protection() -> None:
    """Даже при уже настроенных mp изменение hookscript учитывает protection."""
    config = storage_config()
    volumes = {volume["name"]: volume for volume in config["volumes"]}
    calls: list[tuple[str, ...]] = []

    def fake(*args, check=True):
        calls.append(args)
        if args[:2] == ("pct", "status"):
            return SimpleNamespace(returncode=0, stdout="status: stopped")
        if args[:2] == ("pct", "config"):
            return SimpleNamespace(returncode=0, stdout="protection: 1\n")
        return SimpleNamespace(returncode=0, stdout="")

    with (
        patch.object(storage, "command", side_effect=fake),
        patch.object(storage, "ensure_startup_hook") as hook,
    ):
        storage.configure_lxc(340, volumes, config["mounts"], [], config)
    hook.assert_called_once_with(340, config)
    assert ("pct", "set", "340", "--protection", "0") in calls
    assert ("pct", "set", "340", "--protection", "1") in calls



def test_local_storage_enables_snippets_without_losing_content() -> None:
    """Поддерживаемая команда pvesh читает настройки, pvesm меняет только нужное."""
    calls = []
    config = {
        "storage": "local", "type": "dir", "path": "/var/lib/vz",
        "content": "iso,vztmpl,backup",
    }

    def fake(*args, check=True):
        calls.append(args)
        if args[0] == "pvesh":
            return SimpleNamespace(returncode=0, stdout=json.dumps(config))
        if args[0] == "pvesm" and args[1] == "set":
            return SimpleNamespace(returncode=0, stdout="")
        raise AssertionError(args)

    with patch.object(storage, "command", side_effect=fake):
        storage.ensure_hook_storage()
    assert calls == [
        ("pvesh", "get", "/storage/local", "--output-format", "json"),
        ("pvesm", "set", "local", "--content", "iso,vztmpl,backup,snippets"),
    ]


def test_local_storage_snippets_already_enabled() -> None:
    """Повторный запуск не должен переписывать настройки local."""
    calls = []
    config = {
        "storage": "local", "type": "dir", "path": "/var/lib/vz",
        "content": "backup,snippets,iso",
    }

    def fake(*args, check=True):
        calls.append(args)
        if args[0] != "pvesh":
            raise AssertionError("Уже настроенное хранилище нельзя изменять")
        return SimpleNamespace(returncode=0, stdout=json.dumps(config))

    with patch.object(storage, "command", side_effect=fake):
        storage.ensure_hook_storage()
    assert calls == [("pvesh", "get", "/storage/local", "--output-format", "json")]


def test_local_storage_unknown_configuration_stops_safely() -> None:
    """Не затирать содержимое local при неполном ответе API."""
    for config in (
        {"storage": "local", "type": "lvmthin", "path": "/var/lib/vz", "content": "iso"},
        {"storage": "local", "type": "dir", "path": "/another", "content": "iso"},
        {"storage": "local", "type": "dir", "path": "/var/lib/vz"},
    ):
        with patch.object(
            storage, "command", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(config),
            ),
        ) as fake:
            try:
                storage.ensure_hook_storage()
            except storage.StorageError:
                pass
            else:
                raise AssertionError("Неизвестная конфигурация должна быть отклонена")
        assert fake.call_count == 1


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
            "pve_storage: " + json.dumps({"mounts": storage_config()["mounts"]}) + "\n",
            encoding="utf-8",
        )
        root = Path(tmp)
        schema = ROOT / "infrastructure/schemas/physical-storage.schema.yaml"
        target_schema = root / "infrastructure/schemas/physical-storage.schema.yaml"
        target_schema.parent.mkdir(parents=True)
        target_schema.write_bytes(schema.read_bytes())
        manifest = ROOT / "infrastructure/hosts/pve/storage.yaml"
        target_manifest = root / "infrastructure/hosts/pve/storage.yaml"
        target_manifest.parent.mkdir(parents=True)
        target_manifest.write_bytes(manifest.read_bytes())
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
    test_originals_acl()
    test_acl_install_failure_preserves_photos()
    test_readonly_mount_preserves_originals()
    test_ro_change_protected_guest()
    test_existing_volume_not_formatted()
    test_new_volume_and_interrupted_recovery()
    test_unknown_existing_volume_does_not_format()
    test_guest_mount_paths()
    test_startup_guard_blocks_missing_storage()
    test_no_start_on_incomplete_setup()
    test_protected_guest_keeps_protection_after_success()
    test_protected_guest_restores_protection_after_error()
    test_existing_protected_mounts_do_not_toggle_protection()
    test_protected_guest_hook_install_restores_protection()
    test_local_storage_enables_snippets_without_losing_content()
    test_local_storage_snippets_already_enabled()
    test_local_storage_unknown_configuration_stops_safely()
    test_no_storage_on_other_guests()
    test_guest_storage_sent_to_pve()
    print("OK: Guest storage contract")

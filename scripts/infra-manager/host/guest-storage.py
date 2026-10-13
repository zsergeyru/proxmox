#!/usr/bin/env python3
"""Подготовка одного постоянного тома LXC с каталогами на PVE.

Сценарий получает описание гостя через stdin, выполняется только на PVE и
никогда не форматирует существующий логический том.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


class StorageError(RuntimeError):
    """Постоянное хранилище не соответствует описанию гостя."""


def command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Выполнить системную команду; при ошибке сохранить причину."""
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if check and result.returncode:
        reason = result.stderr.strip() or result.stdout.strip() or f"код {result.returncode}"
        raise StorageError(f"{' '.join(args[:2])}: {reason}")
    return result


def validate(vmid: int, config: dict) -> tuple[str, str, dict, list[dict]]:
    """Проверить описание тома и обоих подключений до записи на диск."""
    if type(vmid) is not int or not 100 <= vmid <= 9999:
        raise StorageError("Некорректный VMID")
    if not isinstance(config, dict):
        raise StorageError("Некорректное описание хранилища")
    vg, pool = config.get("volume_group"), config.get("thin_pool")
    for name in (vg, pool):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_+-]+", name):
            raise StorageError("Недопустимое имя группы томов или пула")

    volume = config.get("volume")
    if not isinstance(volume, dict):
        raise StorageError("Ожидается описание одного общего тома")
    name, size, mountpoint = (
        volume.get("name"), volume.get("size_gb"), volume.get("mountpoint")
    )
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+-]*", name):
        raise StorageError("Недопустимое имя тома")
    if type(size) is not int or not 1 <= size <= 10000:
        raise StorageError("Некорректный размер тома")
    if (
        not isinstance(mountpoint, str)
        or not mountpoint.startswith("/mnt/bindmounts/")
        or not Path(mountpoint).is_absolute()
        or ".." in Path(mountpoint).parts
        or "//" in mountpoint
        or str(Path(mountpoint)) != mountpoint
    ):
        raise StorageError("Недопустимая точка подключения тома PVE")

    mounts = config.get("mounts")
    if not isinstance(mounts, list) or not mounts:
        raise StorageError("Не описаны подключения гостя")
    slots: set[str] = set()
    directories: set[str] = set()
    targets: set[str] = set()
    for item in mounts:
        if not isinstance(item, dict):
            raise StorageError("Некорректная запись подключения")
        slot, directory, target = (
            item.get("slot"), item.get("directory"), item.get("target")
        )
        if slot not in ("mp0", "mp1") or slot in slots:
            raise StorageError("Недопустимый или повторяющийся номер подключения")
        if (
            not isinstance(directory, str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]*", directory)
            or directory in directories
        ):
            raise StorageError("Недопустимый или повторяющийся каталог тома")
        if (
            not isinstance(target, str)
            or not target.startswith("/")
            or target == "/"
            or ".." in Path(target).parts
            or "//" in target
            or str(Path(target)) != target
            or target in targets
        ):
            raise StorageError("Недопустимый или повторяющийся каталог гостя")
        if type(item.get("read_only")) is not bool:
            raise StorageError("Не задан режим доступа к каталогу")
        slots.add(slot)
        directories.add(directory)
        targets.add(target)
    return vg, pool, volume, mounts


def source_path(volume: dict, item: dict) -> Path:
    """Определить путь каталога внутри единственного тома на PVE."""
    return Path(volume["mountpoint"]) / item["directory"]


def existing_mount_config(vmid: int, volume: dict, mounts: list[dict]) -> list[dict]:
    """Не переписывать уже занятые подключения LXC."""
    config = command("pct", "config", str(vmid)).stdout
    if "unprivileged: 1" not in config.splitlines():
        raise StorageError(f"LXC {vmid} должен быть непривилегированным")
    lines = dict(line.split(": ", 1) for line in config.splitlines() if ": " in line)
    pending: list[dict] = []
    for item in mounts:
        old = lines.get(item["slot"])
        if old is None:
            pending.append(item)
            continue
        parts = old.split(",")
        attributes = dict(part.split("=", 1) for part in parts[1:] if "=" in part)
        readonly = attributes.get("ro", "0") == "1"
        if (
            parts[0] != str(source_path(volume, item))
            or attributes.get("mp") != item["target"]
            or readonly != item["read_only"]
        ):
            raise StorageError(f"{item['slot']} уже занят другим подключением: {old}")
    return pending


def ensure_volume(vg: str, pool: str, volume: dict) -> tuple[Path, str]:
    """Проверить существующий том или создать новый с ext4."""
    device = Path(f"/dev/{vg}/{volume['name']}")
    # Список имён позволяет отличить отсутствие тома от ошибки LVM.
    names = command("lvs", "--noheadings", "-o", "lv_name", vg).stdout.split()
    created = volume["name"] not in names
    if created:
        command(
            "lvcreate", "-V", f"{volume['size_gb']}G",
            "-T", f"{vg}/{pool}", "-n", volume["name"],
        )
    else:
        size_result = command(
            "lvs", "--noheadings", "--units", "b", "--nosuffix",
            "-o", "lv_size", str(device),
        ).stdout.strip()
        pool_result = command(
            "lvs", "--noheadings", "-o", "pool_lv", str(device)
        ).stdout.strip()
        try:
            actual_size = int(float(size_result))
        except ValueError as exc:
            raise StorageError(f"Не удалось определить размер тома {device}") from exc
        if pool_result != pool or abs(actual_size - volume["size_gb"] * 1024**3) >= 1024**2:
            raise StorageError(
                f"Том {device} уже существует, но размер или пул отличаются. "
                "Не изменяем существующий том автоматически"
            )

    filesystem = command(
        "blkid", "-s", "TYPE", "-o", "value", str(device), check=False
    )
    if created:
        if filesystem.returncode == 0 or filesystem.stdout.strip():
            raise StorageError(f"Новый том {device} неожиданно содержит файловую систему")
        # На данных фотоархива не нужно резервировать 5% блоков для root.
        command("mkfs.ext4", "-m", "0", str(device))
        filesystem = command("blkid", "-s", "TYPE", "-o", "value", str(device))
    if filesystem.returncode or filesystem.stdout.strip() != "ext4":
        raise StorageError(
            f"Том {device} не имеет ext4. Форматирование существующего тома запрещено"
        )
    uuid = command(
        "blkid", "-s", "UUID", "-o", "value", str(device)
    ).stdout.strip()
    if not re.fullmatch(r"[a-fA-F0-9-]+", uuid):
        raise StorageError(f"Некорректный UUID тома {device}")
    return device, uuid


def ensure_host_mount(device: Path, uuid: str, volume: dict) -> Path:
    """Подключить том на PVE; не скрывать уже лежащие в папке данные."""
    path = Path(volume["mountpoint"])
    if path.is_symlink():
        raise StorageError(f"Вместо каталога обнаружена ссылка: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise StorageError(f"Не каталог: {path}")

    mounted = command(
        "findmnt", "-rn", "-o", "SOURCE", "--mountpoint", str(path), check=False
    )
    if mounted.returncode:
        if any(path.iterdir()):
            raise StorageError(f"Нельзя скрыть существующие файлы: {path}")
    elif os.path.realpath(mounted.stdout.strip()) != os.path.realpath(device):
        raise StorageError(
            f"На {path} подключён другой диск: {mounted.stdout.strip()}"
        )

    fstab = Path("/etc/fstab")
    content = fstab.read_text(encoding="utf-8")
    entries = [
        line.split() for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    entry = f"UUID={uuid} {path} ext4 defaults 0 2"
    matches = [parts for parts in entries if len(parts) >= 2 and parts[1] == str(path)]
    if matches:
        if len(matches) != 1 or matches[0][:3] != [f"UUID={uuid}", str(path), "ext4"]:
            raise StorageError(f"В fstab уже есть другое подключение {path}")
    else:
        with fstab.open("a", encoding="utf-8") as handle:
            if content and not content.endswith("\n"):
                handle.write("\n")
            handle.write(entry + "\n")

    if mounted.returncode:
        command("mount", str(path))
        mounted = command(
            "findmnt", "-rn", "-o", "SOURCE", "--mountpoint", str(path)
        )
    if os.path.realpath(mounted.stdout.strip()) != os.path.realpath(device):
        raise StorageError(f"Том {device} не подключён к {path}")
    return path


def ensure_directories(volume: dict, mounts: list[dict]) -> None:
    """Создать только подкаталоги; существующее содержимое не трогать."""
    for item in mounts:
        path = source_path(volume, item)
        if path.is_symlink():
            raise StorageError(f"Вместо каталога обнаружена ссылка: {path}")
        path.mkdir(exist_ok=True)
        if not path.is_dir():
            raise StorageError(f"Не каталог: {path}")
        if item["read_only"]:
            # Для папки оригиналов право чтения нужно root гостя (uid 100000).
            path.chmod(0o755)
            if item["directory"] == "data":
                originals = path / "originals"
                if originals.is_symlink():
                    raise StorageError(f"Вместо каталога обнаружена ссылка: {originals}")
                originals.mkdir(exist_ok=True)
                originals.chmod(0o755)
        else:
            # Непривилегированный LXC: root гостя соответствует uid/gid 100000.
            os.chown(path, 100000, 100000)
            path.chmod(0o750)


def configure_lxc(vmid: int, volume: dict, mounts: list[dict], pending: list[dict]) -> None:
    """Подключить папки к LXC; при необходимости перезапустить контейнер."""
    active = command("pct", "status", str(vmid)).stdout.strip().endswith("running")
    if pending and active:
        command("pct", "shutdown", str(vmid), "--timeout", "60")
    try:
        for item in pending:
            spec = (
                f"{source_path(volume, item)},mp={item['target']},"
                f"ro={int(item['read_only'])}"
            )
            command("pct", "set", str(vmid), f"--{item['slot']}", spec)
    finally:
        if pending and active:
            command("pct", "start", str(vmid))

    if command("pct", "status", str(vmid)).stdout.strip().endswith("running"):
        for item in mounts:
            test = command(
                "pct", "exec", str(vmid), "--",
                "mountpoint", "-q", item["target"], check=False,
            )
            if test.returncode:
                raise StorageError(
                    f"LXC {vmid}: {item['target']} не подключён после настройки"
                )


def main() -> int:
    vmid = int(sys.argv[1])
    vg, pool, volume, mounts = validate(vmid, json.load(sys.stdin))
    pending = existing_mount_config(vmid, volume, mounts)
    device, uuid = ensure_volume(vg, pool, volume)
    ensure_host_mount(device, uuid, volume)
    ensure_directories(volume, mounts)
    configure_lxc(vmid, volume, mounts, pending)
    print(f"[ОК] LXC {vmid}: постоянный том подключён, mp0/mp1 готовы")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (StorageError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"[ОШИБКА] Подготовка постоянного хранилища: {exc}", file=sys.stderr)
        raise SystemExit(1)

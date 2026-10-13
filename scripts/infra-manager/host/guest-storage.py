#!/usr/bin/env python3
"""PVE-only подготовка постоянных томов LXC без удаления существующих данных.

Получает описание из проверенного проекта через stdin, не читает произвольные
команды гостя и никогда не форматирует уже существующий логический том.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


class StorageError(RuntimeError):
    """Нарушение контракта постоянного хранилища."""


def command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if check and result.returncode:
        reason = result.stderr.strip() or result.stdout.strip() or f"код {result.returncode}"
        raise StorageError(f"{' '.join(args[:2])}: {reason}")
    return result


def validate(vmid: int, config: dict) -> tuple[str, str, list[dict]]:
    """Проверить полный набор путей до изменений на PVE."""
    if not 100 <= vmid <= 9999:
        raise StorageError("Некорректный VMID")
    vg, pool = config.get("volume_group"), config.get("thin_pool")
    mounts = config.get("mounts")
    for name in (vg, pool):
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_+-]+", name):
            raise StorageError("Недопустимое имя группы томов или пула")
    if not isinstance(mounts, list) or not mounts:
        raise StorageError("Не описаны постоянные подключения")
    used_slots: set[str] = set()
    used_lvs: set[str] = set()
    used_paths: set[str] = set()
    for item in mounts:
        if not isinstance(item, dict):
            raise StorageError("Некорректная запись постоянного тома")
        slot = item.get("slot")
        lv = item.get("lv")
        size = item.get("size_gb")
        source, target = item.get("source"), item.get("target")
        if slot not in ("mp0", "mp1") or slot in used_slots:
            raise StorageError("Недопустимый или повторяющийся номер подключения")
        if not isinstance(lv, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_+-]*", lv) or lv in used_lvs:
            raise StorageError("Недопустимое или повторяющееся имя тома")
        if type(size) is not int or not 1 <= size <= 10000:
            raise StorageError("Некорректный размер тома")
        if not isinstance(source, str) or not source.startswith("/mnt/bindmounts/"):
            raise StorageError("Каталог PVE должен быть внутри /mnt/bindmounts")
        if not isinstance(target, str) or not target.startswith("/") or target == "/":
            raise StorageError("Недопустимый каталог гостя")
        for path in (source, target):
            if ".." in Path(path).parts or "//" in path or path in used_paths:
                raise StorageError("Некорректный или повторяющийся путь")
            used_paths.add(path)
        if type(item.get("read_only")) is not bool:
            raise StorageError("Режим чтения тома должен быть задан явно")
        used_slots.add(slot)
        used_lvs.add(lv)
    return vg, pool, mounts


def existing_mount_config(vmid: int, mounts: list[dict]) -> list[dict]:
    """Не подменять чужие подключения mp0/mp1."""
    config = command("pct", "config", str(vmid)).stdout
    if "unprivileged: 1" not in config:
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
        if parts[0] != item["source"] or attributes.get("mp") != item["target"] or readonly != item["read_only"]:
            raise StorageError(f"{item['slot']} уже занят другим подключением: {old}")
    return pending


def ensure_volume(vg: str, pool: str, item: dict) -> tuple[Path, str]:
    """Создать том только при отсутствии; форматировать только созданный сейчас."""
    device = Path(f"/dev/{vg}/{item['lv']}")
    found = command("lvs", "--noheadings", "-o", "lv_name", str(device), check=False)
    created = found.returncode != 0
    if created:
        command("lvcreate", "-V", f"{item['size_gb']}G", "-T", f"{vg}/{pool}", "-n", item["lv"])
    else:
        actual = command("lvs", "--noheadings", "--units", "b", "--nosuffix", "-o", "lv_size", str(device)).stdout.strip()
        try:
            size = int(float(actual))
        except ValueError as exc:
            raise StorageError(f"Не удалось проверить размер существующего {device}") from exc
        if abs(size - item["size_gb"] * 1024**3) >= 1024**2:
            raise StorageError(f"Том {device} уже существует с другим размером: {size} байт")

    filesystem = command("blkid", "-s", "TYPE", "-o", "value", str(device), check=False)
    if created:
        if filesystem.returncode == 0:
            raise StorageError(f"Новый том {device} неожиданно содержит файловую систему")
        command("mkfs.ext4", "-F", str(device))
        filesystem = command("blkid", "-s", "TYPE", "-o", "value", str(device))
    if filesystem.returncode or filesystem.stdout.strip() != "ext4":
        raise StorageError(f"Том {device} не имеет ext4; автоматическое форматирование существующего тома запрещено")
    uuid = command("blkid", "-s", "UUID", "-o", "value", str(device)).stdout.strip()
    if not re.fullmatch(r"[a-fA-F0-9-]+", uuid):
        raise StorageError(f"Некорректный UUID тома {device}")
    return device, uuid


def ensure_host_mount(device: Path, uuid: str, item: dict) -> None:
    """Смонтировать том и сохранить его в fstab без перезаписи иных записей."""
    path = Path(item["source"])
    if path.is_symlink():
        raise StorageError(f"Символьная ссылка вместо каталога: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise StorageError(f"Не каталог: {path}")

    entry = f"UUID={uuid} {path} ext4 defaults 0 2"
    fstab = Path("/etc/fstab")
    content = fstab.read_text(encoding="utf-8")
    current = [line.split() for line in content.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    for tokens in current:
        if len(tokens) >= 2 and str(path) == tokens[1]:
            if tokens[0] != f"UUID={uuid}" or tokens[2] != "ext4":
                raise StorageError(f"В fstab уже задано другое устройство для {path}")
            break
    else:
        with fstab.open("a", encoding="utf-8") as handle:
            if content and not content.endswith("\n"):
                handle.write("\n")
            handle.write(entry + "\n")

    mounted = command("findmnt", "-rn", "-o", "SOURCE", "--mountpoint", str(path), check=False)
    if mounted.returncode:
        if any(path.iterdir()):
            raise StorageError(f"Нельзя скрыть существующие файлы при монтировании {path}")
        command("mount", str(path))
        mounted = command("findmnt", "-rn", "-o", "SOURCE", "--mountpoint", str(path))
    source = mounted.stdout.strip()
    if os.path.realpath(source) != os.path.realpath(device):
        raise StorageError(f"{path} подключён к {source}, а ожидался {device}")

    if item["read_only"]:
        originals = path / "originals"
        originals.mkdir(exist_ok=True)
        originals.chmod(0o755)
        path.chmod(0o755)
    else:
        # Непривилегированный LXC: root гостя соответствует uid/gid 100000 на PVE.
        os.chown(path, 100000, 100000)
        path.chmod(0o750)


def configure_lxc(vmid: int, mounts: list[dict], pending: list[dict]) -> None:
    """Изменить mp только у выбранного контейнера с корректной остановкой."""
    active = command("pct", "status", str(vmid)).stdout.strip().endswith("running")
    if pending and active:
        command("pct", "shutdown", str(vmid), "--timeout", "60")
    try:
        for item in pending:
            spec = f"{item['source']},mp={item['target']},ro={int(item['read_only'])}"
            command("pct", "set", str(vmid), f"--{item['slot']}", spec)
    finally:
        if pending and active:
            command("pct", "start", str(vmid))

    # Проверяем реальное подключение, а не только наличие записи в pct config.
    if command("pct", "status", str(vmid)).stdout.strip().endswith("running"):
        for item in mounts:
            test = command("pct", "exec", str(vmid), "--", "mountpoint", "-q", item["target"], check=False)
            if test.returncode:
                raise StorageError(f"LXC {vmid}: {item['target']} не подключён после настройки")


def main() -> int:
    vmid = int(sys.argv[1])
    config = json.load(sys.stdin)
    vg, pool, mounts = validate(vmid, config)
    pending = existing_mount_config(vmid, mounts)
    for item in mounts:
        device, uuid = ensure_volume(vg, pool, item)
        ensure_host_mount(device, uuid, item)
    configure_lxc(vmid, mounts, pending)
    print(f"[ОК] LXC {vmid}: постоянные тома готовы (mp0/mp1)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (StorageError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"[ОШИБКА] Подготовка постоянного хранилища: {exc}", file=sys.stderr)
        raise SystemExit(1)

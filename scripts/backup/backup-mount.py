#!/usr/bin/env python3
"""Одноразовая привязка заранее проверенного SSD либо NFS назначения PVE.

Изменяет только собственную защищённую backup.yaml и отдельные записи fstab;
не форматирует диски и не подключает неустановленные серверы самостоятельно.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import yaml

CONFIG = Path("/etc/infra-manager/backup/backup.yaml")
FSTAB = Path("/etc/fstab")


class MountError(RuntimeError):
    """Адрес назначения не был надёжно подтверждён."""


def run(argv: list[str], *, capture: bool = True):
    r = subprocess.run(argv, capture_output=capture, text=True, check=False)
    if r.returncode:
        raise MountError(f"Не удалось выполнить {argv[0]} (код {r.returncode})")
    return r.stdout.strip()


def read():
    if CONFIG.is_symlink() or not CONFIG.is_file():
        raise MountError("Не найден локальный backup.yaml")
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("host") != "pve":
        raise MountError("Неверное описание резервных копий PVE")
    return data


def save(config) -> None:
    fd, name = tempfile.mkstemp(prefix=".backup-", dir=CONFIG.parent)
    temporary = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            yaml.safe_dump(config, stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, CONFIG)
    finally:
        temporary.unlink(missing_ok=True)


def add_fstab(entry: str, mountpoint: Path) -> None:
    if FSTAB.is_symlink() or not FSTAB.is_file():
        raise MountError("Не удалось проверить /etc/fstab")
    before = FSTAB.read_text(encoding="utf-8")
    for line in before.splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        fields = line.split()
        if len(fields) > 1 and fields[1] == str(mountpoint):
            if line != entry:
                raise MountError("Для этой точки уже есть другая запись fstab")
            return
    with FSTAB.open("a", encoding="utf-8") as file:
        file.write("\n" + entry + "\n")


def destination(config: dict, name: str) -> dict:
    matching = [item for item in config.get("destinations", [])
                if isinstance(item, dict) and item.get("id") == name]
    if len(matching) != 1:
        raise MountError("Неоднозначное описание назначения")
    return matching[0]


def check_partition(part: Path, serial: str, uuid: str) -> None:
    if not str(part).startswith("/dev/disk/by-id/") or not part.name.endswith("-part1"):
        raise MountError("Нужна постоянная ссылка на первый раздел /dev/disk/by-id")
    if not part.is_symlink():
        raise MountError("Раздел недоступен")
    real = part.resolve(strict=True)
    value = run(["lsblk", "-n", "-o", "TYPE,FSTYPE,UUID", str(real)]).split()
    if value != ["part", "ext4", uuid]:
        raise MountError("Раздел или UUID ext4 не совпадает")
    disk = Path(str(part)[:-6])
    if not disk.is_symlink():
        raise MountError("Не найден родительский диск")
    disk_serial = run(["lsblk", "-dn", "-o", "SERIAL", str(disk)]).strip()
    if disk_serial != serial:
        raise MountError("Серийный номер SSD не совпадает")
    if run(["blkid", "-s", "UUID", "-o", "value", str(real)]) != uuid:
        raise MountError("UUID раздела изменился")


def configure_fast(part: Path, serial: str, uuid: str) -> None:
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
        raise MountError("Некорректный UUID")
    check_partition(part, serial, uuid)
    config = read()
    target = destination(config, "fast-ssd")
    point = Path(target["mountpoint"])
    if target.get("uuid") not in (None, uuid):
        raise MountError("В конфигурации сохранён другой SSD")
    if point.is_symlink():
        raise MountError("Ссылка вместо каталога назначения запрещена")
    point.mkdir(parents=True, exist_ok=True)
    if any(point.iterdir()) and not point.is_mount():
        raise MountError("Каталог назначения не пуст и диск не подключён")
    entry = f"UUID={uuid} {point} ext4 defaults,nofail,x-systemd.device-timeout=15s 0 2"
    add_fstab(entry, point)
    if not point.is_mount():
        run(["mount", str(point)])
    source = run(["findmnt", "-M", str(point), "-n", "-o", "UUID"])
    if source != uuid:
        raise MountError("После подключения UUID не совпадает")
    target["uuid"] = uuid
    target["source"] = None
    save(config)
    print("[ОК] Быстрый SSD подключён; UUID сохранён в локальном backup.yaml")


def configure_nas(source: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9.:-]+:/[A-Za-z0-9/_-]+", source):
        raise MountError("Нужен проверенный NFSv4 сервер и абсолютный путь экспорта")
    config = read()
    target = destination(config, "nas-backup")
    if target.get("source") not in (None, source):
        raise MountError("В конфигурации записан другой NAS")
    point = Path(target["mountpoint"])
    if point.is_symlink():
        raise MountError("Нельзя использовать символическую ссылку для NAS")
    point.mkdir(parents=True, exist_ok=True)
    if any(point.iterdir()) and not point.is_mount():
        raise MountError("Каталог NAS не пуст без подключения NFS")
    line = f"{source} {point} nfs4 rw,nofail,_netdev,x-systemd.automount,noauto 0 0"
    add_fstab(line, point)
    if not point.is_mount():
        run(["mount", "-t", "nfs4", source, str(point)])
    actual = run(["findmnt", "-M", str(point), "-n", "-o", "SOURCE"])
    fstype = run(["findmnt", "-M", str(point), "-n", "-o", "FSTYPE"])
    if actual != source or fstype != "nfs4":
        raise MountError("Сетевой том NAS не подтверждён")
    target["source"] = source
    target["uuid"] = None
    save(config)
    print("[ОК] NAS NFSv4 подключён и зафиксирован в защищённой конфигурации")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    fast = subs.add_parser("fast")
    fast.add_argument("--partition", type=Path, required=True)
    fast.add_argument("--serial", required=True)
    fast.add_argument("--uuid", required=True)
    nas = subs.add_parser("nas")
    nas.add_argument("--source", required=True)
    a = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Только root на PVE")
    try:
        if a.action == "fast":
            configure_fast(a.partition, a.serial, a.uuid)
        else:
            configure_nas(a.source)
    except (MountError, OSError, ValueError, KeyError, yaml.YAMLError) as exc:
        print(f"[ОШИБКА] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

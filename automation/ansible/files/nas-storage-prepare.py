#!/usr/bin/env python3
"""Подготовка новых HDD с сохранением уже подключённого ext4 физического NAS.

Опасные операции разрешены только после подтверждения PVE и повторной
проверки модели, номера, объёма, сигнатур и привязки /dev/disk/by-id.
Системный SSD никогда не передаётся этому исполнителю. При сбое остаётся
журнал: автоматически форматировать диск повторно запрещено.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

JOURNAL = Path("/etc/nas-apps/storage-preparation.json")
ROLES = {"shared": ("create-ext4", "ST1000DM003", 1000),
         "data": ("create-zfs", "ST4000VX016", 4000),
         "backup": ("create-zfs", "ST4000VX016", 4000)}
MARKER = "NAS_STORAGE_RESULT="


class StorageError(RuntimeError):
    """Отказ без очистки существующих данных."""


def execute(*argv: str, allowed: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess[str]:
    """Запуск без оболочки и без подмены аргументов оператора."""

    result = subprocess.run(argv, text=True, capture_output=True, check=False)
    if result.returncode not in allowed:
        raise StorageError(f"Не выполнилась команда {argv[0]} (код {result.returncode})")
    return result


def disk_list() -> list[dict[str, object]]:
    """Прочитать аппаратную топологию на самом NAS перед изменениями."""

    data = json.loads(execute(
        "lsblk", "--json", "--tree", "--bytes", "--output",
        "NAME,PATH,MODEL,SERIAL,SIZE,TYPE,FSTYPE,UUID,PTTYPE,MOUNTPOINTS",
    ).stdout)
    items = data.get("blockdevices")
    if not isinstance(items, list):
        raise StorageError("lsblk не перечислил диски")
    return [item for item in items if item.get("type") == "disk"]


def has_root(disk: dict[str, object]) -> bool:
    """Запретить системный диск даже при ошибочном назначении."""

    def includes_root(part: dict[str, object]) -> bool:
        if "/" in (part.get("mountpoints") or []):
            return True
        return any(includes_root(child) for child in part.get("children") or [])
    return includes_root(disk)


def stable_device(path: str, serial: str) -> str:
    """Разрешить только ссылку by-id на тот же физический диск."""

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", serial):
        raise StorageError("Недопустимый серийный номер HDD")
    paths = [
        candidate for candidate in Path("/dev/disk/by-id").glob("*")
        if candidate.name.startswith(("ata-", "scsi-"))
        and "-part" not in candidate.name
        and serial.lower() in candidate.name.lower()
        and os.path.realpath(candidate) == os.path.realpath(path)
    ]
    paths.sort(key=lambda item: (not item.name.startswith("ata-"), str(item)))
    if not paths:
        raise StorageError(f"Не найден устойчивый путь для HDD {serial}")
    return str(paths[0])


def clean_disk(disk: dict[str, object], stable: str) -> None:
    """До записи отклонить разделы, файловые системы и любые сигнатуры."""

    if disk.get("pttype") or disk.get("fstype") or disk.get("uuid"):
        raise StorageError("У HDD уже есть таблица разделов или файловая система")
    if disk.get("children"):
        raise StorageError("На HDD найдены разделы; форматирование запрещено")
    if any(point for point in disk.get("mountpoints") or []):
        raise StorageError("HDD используется как подключённый том")
    probe = execute("wipefs", "--no-act", stable)
    if probe.stdout.strip():
        raise StorageError("Обнаружены сигнатуры: диск нельзя считать пустым")
    probe = execute("blkid", "-p", stable, allowed=(0, 2))
    if probe.returncode != 2 or probe.stdout.strip():
        raise StorageError("Не подтверждено отсутствие скрытых сигнатур HDD")


def preflight(items: list[dict[str, object]]) -> dict[str, str]:
    """Проверить ВЕСЬ план до первой операции записи."""

    disks = disk_list()
    if sum(has_root(disk) for disk in disks) != 1:
        raise StorageError("Не определён единственный системный SSD")
    matched: dict[str, str] = {}
    taken: set[str] = set()
    if not isinstance(items, list) or len(items) != 3 or not all(
        isinstance(item, dict) for item in items
    ) or {item.get("role") for item in items} != set(ROLES):
        raise StorageError("Для подготовки нужны ровно три объявленные роли HDD")
    for item in items:
        role = item["role"]
        action, expected_model, expected_gb = ROLES[role]
        serial = item.get("serial")
        requested = item.get("action")
        if (requested != action and not (role == "shared" and requested == "preserve")) or not isinstance(serial, str):
            raise StorageError(f"Операция для {role} не соответствует плану")
        match = [disk for disk in disks if disk.get("serial") == serial]
        if len(match) != 1:
            raise StorageError(f"Нет однозначного серийного номера {role}: {serial}")
        disk = match[0]
        if has_root(disk):
            raise StorageError("Попытка подготовки системного SSD запрещена")
        if not isinstance(disk.get("model"), str) or expected_model.lower() not in disk["model"].lower():
            raise StorageError(f"Изменилась модель диска {role}")
        size = disk.get("size")
        if not isinstance(size, int) or abs(size / 10**9 - expected_gb) > expected_gb * .05:
            raise StorageError(f"Изменилась ёмкость диска {role}")
        device = stable_device(str(disk["path"]), serial)
        if device in taken:
            raise StorageError("Один HDD назначен двум ролям")
        if requested == "preserve":
            if not disk.get("children") and not disk.get("fstype"):
                raise StorageError("Сохраняемый HDD shared не содержит файловую систему")
        else:
            clean_disk(disk, device)
        matched[role] = device
        taken.add(device)
    pools = execute("zpool", "list", "-H", "-o", "name", allowed=(0, 1))
    if pools.returncode == 0 and any(name in pools.stdout.splitlines()
                                      for name in ("nas-data", "nas-backup")):
        raise StorageError("Пул nas-data или nas-backup уже существует")
    for target in ("/nas-data", "/nas-backup"):
        mount = execute("findmnt", "--mountpoint", target, allowed=(0, 1))
        if mount.returncode == 0:
            raise StorageError(f"Точка подключения уже используется: {target}")
        directory = Path(target)
        if directory.is_symlink() or (
            directory.exists() and (
                not directory.is_dir() or any(directory.iterdir())
            )
        ):
            raise StorageError(
                f"Каталог {target} не пуст или является ссылкой; "
                "нельзя скрывать существующие данные монтированием ZFS"
            )
    return matched


def inspect_preserved_shared(serial: str, device: str) -> dict[str, str]:
    """Только чтение: подтвердить существующий ext4, его устройство, OMV и mount.

    Любая неоднозначность запрещает создание двух ZFS-пулов. Раздел shared
    не форматируется, не переподключается и не заменяется.
    """

    disks = [disk for disk in disk_list() if disk.get("serial") == serial]
    if len(disks) != 1 or has_root(disks[0]):
        raise StorageError("Существующий HDD shared не идентифицирован однозначно")
    disk = disks[0]
    if stable_device(str(disk.get("path")), serial) != device:
        raise StorageError("У HDD shared изменился постоянный путь")
    children = disk.get("children") or []
    if children:
        if len(children) != 1 or children[0].get("type") != "part" or children[0].get("children"):
            raise StorageError("HDD shared содержит неоднозначный набор разделов")
        fs = children[0]
    else:
        fs = disk
    uid = fs.get("uuid")
    source = fs.get("path")
    if (
        fs.get("fstype") != "ext4"
        or not isinstance(uid, str)
        or not re.fullmatch(r"[a-fA-F0-9-]{16,40}", uid)
        or not isinstance(source, str)
        or not source.startswith("/dev/")
    ):
        raise StorageError("Существующий HDD shared не является однозначным томом ext4")
    # Само наличие раздела не доказывает, что именно этот раздел смонтирован.
    found = execute("blkid", "-s", "UUID", "-o", "value", source).stdout.strip()
    if found != uid:
        raise StorageError("UUID существующего ext4 не совпадает с проверенным разделом")
    mountpoint = "/srv/dev-disk-by-uuid-" + uid
    records = [
        entry for entry in omv_records()
        if entry.get("dir") == mountpoint
    ]
    if len(records) != 1 or records[0].get("type") != "ext4":
        raise StorageError(
            "Существующий HDD shared не зарегистрирован как ext4 в OMV; "
            "диски не изменены"
        )
    record = records[0]
    ref = record.get("uuid")
    fsname = record.get("fsname")
    if (
        not isinstance(ref, str)
        or not re.fullmatch(r"[a-fA-F0-9-]{36}", ref)
        or not isinstance(fsname, str)
        or os.path.realpath(fsname) != os.path.realpath(source)
    ):
        raise StorageError("Запись ext4 в OMV не соответствует устройству HDD shared")
    response = execute(
        "findmnt", "--json", "--mountpoint", mountpoint,
        "--output", "TARGET,SOURCE,FSTYPE,UUID",
    )
    try:
        fs_mounts = json.loads(response.stdout).get("filesystems")
    except (ValueError, AttributeError) as exc:
        raise StorageError("Не удалось прочитать точку подключения ext4") from exc
    if not isinstance(fs_mounts, list) or len(fs_mounts) != 1:
        raise StorageError("Диск shared не имеет отдельного подключения ext4")
    mounted = fs_mounts[0]
    if (
        mounted.get("target") != mountpoint
        or mounted.get("fstype") != "ext4"
        or mounted.get("uuid") != uid
        or not isinstance(mounted.get("source"), str)
        or os.path.realpath(mounted["source"]) != os.path.realpath(source)
    ):
        raise StorageError("Том shared подключён не с ожидаемого HDD/UUID")
    return {"mountpoint": mountpoint, "filesystem_uuid": uid, "omv_reference": ref}


def save_journal(data: dict[str, object]) -> None:
    """Зафиксировать попытку ДО форматирования; не позволять повторить её."""

    JOURNAL.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if JOURNAL.exists():
        raise StorageError(
            "Найдена предыдущая попытка подготовки: автоматический повтор "
            "разрушающих операций запрещён. Проверьте её состояние."
        )
    fd = os.open(JOURNAL, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


def filesystem_uuid(path: str) -> str:
    """Определить UUID после форматирования через штатный blkid."""

    uid = execute("blkid", "-s", "UUID", "-o", "value", path).stdout.strip()
    if not re.fullmatch(r"[a-fA-F0-9-]{16,40}", uid):
        raise StorageError("Невозможно прочитать UUID созданной ext4")
    return uid


def pool_guid(name: str) -> str:
    """Прочитать устойчивую идентичность нового пула ZFS."""

    guid = execute("zpool", "get", "-H", "-o", "value", "guid", name).stdout.strip()
    if not re.fullmatch(r"[0-9]{1,20}", guid):
        raise StorageError(f"Некорректный GUID пула {name}")
    return guid


def omv_records() -> list[dict[str, object]]:
    """Проверить формат постоянной базы OMV без прямого изменения XML."""

    response = json.loads(execute(
        "omv-confdbadm", "read", "conf.system.filesystem.mountpoint"
    ).stdout)
    if isinstance(response, dict):
        response = [response]
    if not isinstance(response, list):
        raise StorageError("OMV не вернула корректную базу томов")
    return response


def register_omv(role: str, filesystem: str, source: str, path: str) -> str:
    """Создать новую запись только при отсутствии любых конфликтов."""

    records = omv_records()
    match = [entry for entry in records
             if entry.get("dir") == path or entry.get("fsname") == source]
    if match:
        if len(match) == 1 and all((
            match[0].get("fsname") == source,
            match[0].get("dir") == path,
            match[0].get("type") == filesystem,
            isinstance(match[0].get("uuid"), str),
        )):
            # Расширение ZFS может автоматически зарегистрировать созданный пул.
            return match[0]["uuid"]
        raise StorageError(
            f"В OMV уже зарегистрирован другой том {role}; замена запрещена"
        )
    # OMV: произвольный UUID означает уже существующий объект,
    # а специальный OMV_CONFIGOBJECT_NEW_UUID создаётся самим confdbadm.
    # Не передаём uuid: Object() использует именно этот служебный UUID
    # и автоматически назначает постоянный UUID при первом db.set().
    if filesystem == "ext4":
        entry = {
            "fsname": source, "dir": path, "type": "ext4",
            "opts": "defaults,nofail,user_xattr,acl",
            "freq": 0, "passno": 2, "hidden": False,
        }
    else:
        entry = {
            "fsname": source, "dir": path, "type": "zfs",
            "opts": "rw,relatime,xattr,noacl,casesensitive",
            "freq": 0, "passno": 0, "hidden": True,
            "usagewarnthreshold": 0, "comment": "",
        }
    execute(
        "omv-confdbadm", "update", "conf.system.filesystem.mountpoint",
        json.dumps(entry, ensure_ascii=False),
    )
    # UUID для нового объекта присваивается OMV, а не нашим кодом.
    created = [
        record for record in omv_records()
        if record.get("dir") == path or record.get("fsname") == source
    ]
    if (
        len(created) != 1
        or created[0].get("dir") != path
        or created[0].get("fsname") != source
        or created[0].get("type") != filesystem
        or not isinstance(created[0].get("uuid"), str)
        or not re.fullmatch(r"[a-fA-F0-9-]{36}", created[0]["uuid"])
    ):
        raise StorageError("OMV не подтвердила регистрацию нового тома")
    return created[0]["uuid"]


def verify_mount(path: str, filesystem: str, source: str) -> None:
    """Не принять корневой SSD за отсутствующий диск."""

    result = json.loads(execute(
        "findmnt", "--json", "--mountpoint", path,
        "--output", "TARGET,SOURCE,FSTYPE,UUID",
    ).stdout)
    entries = result.get("filesystems", [])
    if len(entries) != 1 or entries[0].get("target") != path:
        raise StorageError(f"Том не подключён: {path}")
    entry = entries[0]
    if entry.get("fstype") != filesystem:
        raise StorageError(f"Тип ФС в {path} не соответствует плану")
    if filesystem == "ext4" and entry.get("uuid") != source:
        raise StorageError("Подключённый ext4 UUID отличается от созданного")
    if filesystem == "zfs" and entry.get("source", "").split("/")[0] != source:
        raise StorageError("Подключён чужой пул ZFS")


def apply(items: list[dict[str, object]], fingerprint: str) -> dict[str, object]:
    """Сохранять действующий ext4 и создавать только подтверждённые пустые диски."""

    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise StorageError("Не передан проверенный отпечаток плана")
    if JOURNAL.exists():
        raise StorageError(
            "На NAS уже есть журнал подготовки дисков. "
            "Без проверки прошлого запуска повтор не допускается."
        )
    paths = preflight(items)
    shared_item = next(item for item in items if item["role"] == "shared")
    preserve_shared = shared_item["action"] == "preserve"
    # Обязательно проверить существующий ext4 ДО любой записи и создания журнала.
    existing_shared = (
        inspect_preserved_shared(shared_item["serial"], paths["shared"])
        if preserve_shared else None
    )
    save_journal({"fingerprint": fingerprint, "items": items,
                  "state": "started"})
    # После создания журнала никогда не повторять опасную операцию автоматически.
    if existing_shared is None:
        byid = paths["shared"]
        execute("sgdisk", "--new=1:1MiB:0", "--typecode=1:8300",
                "--change-name=1:nas-shared", byid)
        execute("udevadm", "settle")
        part = byid + "-part1"
        if not Path(part).exists():
            raise StorageError("Не найден раздел подготовленного HDD 1 ТБ")
        execute("mkfs.ext4", "-L", "nas-shared", part)
        ext_uuid = filesystem_uuid(part)
        shared_path = "/srv/dev-disk-by-uuid-" + ext_uuid
        shared_source = "/dev/disk/by-uuid/" + ext_uuid
        existing = Path(shared_path)
        if existing.is_symlink() or (
            existing.exists() and (
                not existing.is_dir() or any(existing.iterdir())
            )
        ):
            raise StorageError(
                "На системном диске уже есть данные по пути будущего тома ext4"
            )
    else:
        ext_uuid = existing_shared["filesystem_uuid"]
        shared_path = existing_shared["mountpoint"]
        shared_source = "/dev/disk/by-uuid/" + ext_uuid
    for role, name in (("data", "nas-data"), ("backup", "nas-backup")):
        execute("zpool", "create", "-o", "ashift=12",
                "-O", "compression=lz4", "-O", "atime=off",
                "-m", "/" + name, name, paths[role])

    refs: dict[str, str] = {}
    refs["shared"] = (
        existing_shared["omv_reference"] if existing_shared is not None
        else register_omv("shared", "ext4", shared_source, shared_path)
    )
    for role, name in (("data", "nas-data"), ("backup", "nas-backup")):
        refs[role] = register_omv(role, "zfs", name, "/" + name)
    execute("omv-salt", "deploy", "run", "fstab")
    # fstab в разных сборках OMV может не выполнить само монтирование ext4.
    if not preserve_shared and execute(
        "findmnt", "--mountpoint", shared_path, allowed=(0, 1)
    ).returncode:
        execute("mount", shared_path)
    verify_mount(shared_path, "ext4", ext_uuid)
    verify_mount("/nas-data", "zfs", "nas-data")
    verify_mount("/nas-backup", "zfs", "nas-backup")
    volumes = {
        "shared": {"mountpoint": shared_path, "filesystem_uuid": ext_uuid,
                   "omv_reference": refs["shared"]},
        "data": {"mountpoint": "/nas-data", "pool": "nas-data",
                 "pool_guid": pool_guid("nas-data"),
                 "omv_reference": refs["data"]},
        "backup": {"mountpoint": "/nas-backup", "pool": "nas-backup",
                   "pool_guid": pool_guid("nas-backup"),
                   "omv_reference": refs["backup"]},
    }
    result = {"volumes": volumes, "serials": {
        item["role"]: item["serial"] for item in items
    }, "fingerprint": fingerprint}
    # Сохранить проверенные результаты для разбирательства при потере управляющего гостя.
    # Журнал первоначальной попытки не удаляется и не разрешает повторную запись.
    complete = JOURNAL.with_name("storage-preparation-result.json")
    with complete.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    return result


def recover_from_journal(items: list[dict[str, object]],
                         fingerprint: str) -> dict[str, object]:
    """Продолжить только регистрацию ранее созданных томов, БЕЗ форматирования."""
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise StorageError("Неверный отпечаток первого плана HDD")
    if JOURNAL.is_symlink() or not JOURNAL.is_file():
        raise StorageError("Нет обычного файла журнала первой подготовки")
    try:
        previous = json.loads(JOURNAL.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StorageError("Нельзя прочитать журнал первоначальной подготовки") from exc
    if (not isinstance(previous, dict)
            or set(previous) != {"fingerprint", "items", "state"}
            or previous["state"] != "started"
            or previous["fingerprint"] != fingerprint
            or previous["items"] != items
            or not isinstance(items, list)
            or len(items) != 3):
        raise StorageError("Подтверждение не совпадает с журналом первой подготовки")
    roles = {item.get("role") for item in items if isinstance(item, dict)}
    if roles != set(ROLES):
        raise StorageError("Журнал содержит неверный набор ролей HDD")
    for item in items:
        if (not isinstance(item, dict)
                or set(item) != {"role", "serial", "action"}
                or not isinstance(item["serial"], str)
                or item["action"] != ROLES[item["role"]][0]):
            raise StorageError("Журнал не содержит ожидаемое создание трёх HDD")

    disks = disk_list()
    if sum(has_root(d) for d in disks) != 1:
        raise StorageError("Не определён единственный системный SSD")
    found = {}
    for item in items:
        role = item["role"]
        matches = [d for d in disks if d.get("serial") == item["serial"]]
        if len(matches) != 1 or has_root(matches[0]):
            raise StorageError("Не совпали физические HDD первоначального плана")
        disk = matches[0]
        _action, model, capacity = ROLES[role]
        if (not isinstance(disk.get("model"), str)
                or model.lower() not in disk["model"].lower()
                or not isinstance(disk.get("size"), int)
                or abs(disk["size"] / 1e9 - capacity) > capacity * .05
                or disk.get("pttype") != "gpt"):
            raise StorageError("Изменились модель, ёмкость или разметка HDD")
        by_id = stable_device(str(disk.get("path")), item["serial"])
        found[role] = (disk, by_id, item["serial"])
    if len({part[1] for part in found.values()}) != 3:
        raise StorageError("Неоднозначное соответствие HDD и ролей")

    shared, shared_id, _shared_serial = found["shared"]
    partitions = shared.get("children") or []
    if (len(partitions) != 1
            or partitions[0].get("type") != "part"
            or partitions[0].get("fstype") != "ext4"
            or partitions[0].get("children")):
        raise StorageError("Исходный HDD shared не содержит ровно один ext4-раздел")
    partition = shared_id + "-part1"
    ext_uuid = partitions[0].get("uuid")
    if (not Path(partition).exists()
            or not isinstance(ext_uuid, str)
            or not re.fullmatch(r"[0-9a-fA-F-]{36}", ext_uuid)
            or execute("blkid", "-s", "UUID", "-o", "value", partition).stdout.strip() != ext_uuid
            or execute("blkid", "-s", "LABEL", "-o", "value", partition).stdout.strip() != "nas-shared"):
        raise StorageError("Не совпали UUID, метка или раздел HDD shared")

    shared_path = "/srv/dev-disk-by-uuid-" + ext_uuid
    shared_source = "/dev/disk/by-uuid/" + ext_uuid
    target = Path(shared_path)
    if target.is_symlink():
        raise StorageError("Точка подключения shared является символической ссылкой")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        verify_mount(shared_path, "ext4", ext_uuid)
    pool_info = {}
    for role in ("data", "backup"):
        disk, _stable, serial = found[role]
        parts = disk.get("children") or []
        if (len(parts) != 2
                or sum(part.get("fstype") == "zfs_member" for part in parts) != 1):
            raise StorageError(f"Неверные разделы существующего ZFS-диска {role}")
        pool = "nas-" + role
        status = execute("zpool", "status", "-P", pool).stdout
        if (f"pool: {pool}" not in status
                or not re.search(r"(?m)^\s*state: ONLINE\s*$", status)):
            raise StorageError(f"Пул {pool} отсутствует или не ONLINE")
        leaves = [
            line.split()[0] for line in status.splitlines()
            if re.match(r"^\s+/dev/\S+\s+ONLINE(?:\s|$)", line)
        ]
        if len(leaves) != 1 or serial not in leaves[0]:
            raise StorageError(f"Пул {pool} не принадлежит подтверждённому HDD")
        verify_mount("/" + pool, "zfs", pool)
        pool_info[role] = {"pool": pool, "mountpoint": "/" + pool,
                           "pool_guid": pool_guid(pool)}

    # ЗДЕСЬ НЕ БЫВАЕТ sgdisk/mkfs/zpool create; только штатные настройки OMV.
    refs = {
        "shared": register_omv("shared", "ext4", shared_source, shared_path),
        "data": register_omv("data", "zfs", "nas-data", "/nas-data"),
        "backup": register_omv("backup", "zfs", "nas-backup", "/nas-backup"),
    }
    execute("omv-salt", "deploy", "run", "fstab")
    if execute("findmnt", "--mountpoint", shared_path,
               allowed=(0, 1)).returncode:
        execute("mount", shared_path)
    verify_mount(shared_path, "ext4", ext_uuid)
    verify_mount("/nas-data", "zfs", "nas-data")
    verify_mount("/nas-backup", "zfs", "nas-backup")
    result = {
        "fingerprint": fingerprint,
        "serials": {item["role"]: item["serial"] for item in items},
        "volumes": {
            "shared": {"mountpoint": shared_path,
                       "filesystem_uuid": ext_uuid,
                       "omv_reference": refs["shared"]},
            "data": {**pool_info["data"], "omv_reference": refs["data"]},
            "backup": {**pool_info["backup"], "omv_reference": refs["backup"]},
        },
    }
    complete = JOURNAL.with_name("storage-preparation-result.json")
    if complete.is_symlink():
        raise StorageError("Итоговый отчёт является символической ссылкой")
    if complete.exists():
        try:
            old_result = json.loads(complete.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StorageError("Нельзя прочитать прежний итоговый отчёт") from exc
        if old_result != result:
            raise StorageError("Повторное состояние не совпадает с итоговым отчётом")
    else:
        with complete.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
    return result


def main() -> int:
    """Проверить формат доверенного задания и возвратить только результат."""

    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict) or set(payload) != {"items", "fingerprint"}:
            raise StorageError("Некорректная форма подтверждённого задания")
        if sys.argv[1:] == ["recover"]:
            result = recover_from_journal(payload["items"], payload["fingerprint"])
        elif len(sys.argv) == 1:
            result = apply(payload["items"], payload["fingerprint"])
        else:
            raise StorageError("Неизвестный режим подготовки HDD")
        print(MARKER + json.dumps(result, sort_keys=True))
    except (OSError, ValueError, StorageError, subprocess.SubprocessError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

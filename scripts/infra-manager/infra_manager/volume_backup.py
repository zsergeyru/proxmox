"""Контракт универсального резервирования томов из двух машинных YAML.

Модуль только читает Git и строит ограниченный план. PVE всегда повторно
проверяет фактический том и подключения; план не разрешает создание диска.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from .common import InfraManagerError
from .storage_catalog import load_physical_storage

ID = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
DESTINATIONS = frozenset({"fast-ssd", "nas-backup"})
CONSISTENCY = "offline_lxc"


def load_jobs(repo_root: Path, backup: dict) -> list[dict]:
    """Вернуть проверенные имена томов, их пути и всех объявленных потребителей."""
    section = backup.get("volume_backups")
    if section is None:
        return []
    if (not isinstance(section, dict) or section.get("schema_version") != 1
            or set(section) != {"schema_version", "defaults", "volumes"}):
        raise InfraManagerError("Неверный раздел volume_backups в backup.yaml")
    defaults = section["defaults"]
    if (not isinstance(defaults, dict)
            or set(defaults) != {"enabled", "schedule", "destinations", "method"}
            or defaults["enabled"] is not True
            or defaults["schedule"] != "daily"
            or defaults["method"] != "lvm_snapshot"
            or not isinstance(defaults["destinations"], list)
            or not defaults["destinations"]
            or len(defaults["destinations"]) != len(set(defaults["destinations"]))
            or set(defaults["destinations"]) != DESTINATIONS):
        raise InfraManagerError("Недопустимые общие правила резервирования томов")
    raw_volumes = section["volumes"]
    if not isinstance(raw_volumes, list) or not raw_volumes or len(raw_volumes) > 32:
        raise InfraManagerError("Не указан корректный перечень резервируемых томов")
    storage = load_physical_storage(repo_root, "pve")
    mounts = {item["volume"]: item["path"] for item in storage.mounts}
    requested: set[str] = set()
    jobs = []
    for item in raw_volumes:
        if not isinstance(item, dict) or set(item) != {"id"}:
            raise InfraManagerError("Том резервирования допускает только поле id")
        name = item["id"]
        if not isinstance(name, str) or not ID.fullmatch(name) or name in requested:
            raise InfraManagerError("Неизвестное либо дублирующееся имя тома")
        requested.add(name)
        volume = storage.volume(name)
        mountpoint = mounts.get(name)
        if (volume["kind"] != "lvm_thin" or volume["volume_group"] != "pve"
                or volume["thin_pool"] != "data" or volume["filesystem"] != "ext4"
                or not isinstance(mountpoint, str)
                or mountpoint != "/mnt/bindmounts/" + name.replace("_", "-")):
            raise InfraManagerError("Том не соответствует безопасному контракту PVE LVM")
        jobs.append({"id": name, "mountpoint": mountpoint, "consumers": []})

    # Требование согласованности принадлежит гостю, а не настройке диска.
    guests_dir = repo_root / "infrastructure/guests"
    for file in sorted(guests_dir.glob("*/provision.yaml")):
        if file.is_symlink() or not file.is_file():
            raise InfraManagerError("Недоверенное описание гостя")
        guest = yaml.safe_load(file.read_text(encoding="utf-8"))
        if not isinstance(guest, dict):
            raise InfraManagerError("Неверное описание гостя")
        vmid = guest.get("guest_vmid")
        items = guest.get("pve_storage", {}).get("mounts", [])
        if not isinstance(items, list):
            raise InfraManagerError("Некорректные подключения гостя")
        for item in items:
            if not isinstance(item, dict):
                raise InfraManagerError("Некорректное подключение тома")
            for job in jobs:
                if item.get("volume") != job["id"]:
                    continue
                if (type(vmid) is not int or not 100 <= vmid <= 9999
                        or guest.get("persistence", {}).get("backup_consistency")
                        != CONSISTENCY):
                    raise InfraManagerError(
                        f"Не задана согласованная стратегия гостя для тома {job['id']}"
                    )
                if vmid not in job["consumers"]:
                    job["consumers"].append(vmid)
    for job in jobs:
        job["consumers"].sort()
    return jobs


def due(store, volume_id: str, destinations: list[str]) -> bool:
    """Новая копия ежедневно или при появлении второго назначения."""
    from datetime import UTC, datetime
    from .operation_store import _read
    path = store.settings / ("backup-volume-" + volume_id + ".json")
    try:
        record = _read(path)
    except (InfraManagerError, OSError, ValueError):
        return True
    successes = record.get("destination_dates", {}) if isinstance(record, dict) else {}
    today = datetime.now(UTC).date().isoformat()
    return any(successes.get(name) != today for name in destinations)

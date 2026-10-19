"""Постоянное подтверждённое состояние дисков NAS на управляющем госте.

Не размещается в Git-снимке и не содержит секретов. При отсутствии состояния
не допускается угадывать, какие существующие диски можно переформатировать.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import replace
from pathlib import Path

from .common import InfraManagerError
from .physical_hosts import PhysicalHost
from .physical_host_state import checked_record_path
from .settings import PATHS

def storage_state_path(host: PhysicalHost) -> Path:
    """Хранить идентификаторы отдельно от одноразовой отметки SSH."""

    return checked_record_path(host.name, storage=True, data_root=PATHS.data_dir)


def apply_saved_storage(host: PhysicalHost) -> PhysicalHost:
    """При восстановлении состояния требовать совпадения машины и дисков."""

    path = storage_state_path(host)
    if not path.exists():
        return host
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InfraManagerError("Нельзя прочитать сохранённую идентичность дисков NAS") from exc
    if (
        not isinstance(data, dict)
        or data.get("name") != host.name
        or data.get("address") != host.address
        or data.get("fingerprint") != host.ssh_host_fingerprint
        or not isinstance(data.get("devices"), list)
        or not isinstance(data.get("volumes"), dict)
    ):
        raise InfraManagerError("Сохранённое назначение HDD не соответствует NAS")

    devices = {entry.get("role"): entry for entry in data["devices"]
               if isinstance(entry, dict)}
    if len(devices) != len(host.devices):
        raise InfraManagerError("Сохранён неполный набор накопителей NAS")
    resolved = []
    for entry in host.devices:
        saved = devices.get(entry["role"])
        if not isinstance(saved, dict) or not isinstance(saved.get("serial"), str):
            raise InfraManagerError("Нет подтверждённого серийного номера NAS")
        if any(saved.get(field) != entry.get(field) for field in ("role", "model", "capacity_gb")):
            raise InfraManagerError("Модель или объём назначенного HDD изменились")
        if entry.get("serial") not in (None, saved["serial"]):
            raise InfraManagerError("Номер диска изменился по сравнению с storage.yaml")
        resolved.append({**entry, "serial": saved["serial"]})
    volumes = data["volumes"]
    for role in ("shared", "data", "backup"):
        configured = (host.volumes or {}).get(role)
        recorded = volumes.get(role)
        if not isinstance(configured, dict) or not isinstance(recorded, dict):
            raise InfraManagerError("Нет постоянного назначения тома NAS")
        for key, value in configured.items():
            if value is not None and recorded.get(key) != value:
                raise InfraManagerError(f"Том {role} не соответствует storage.yaml: {key}")
    return replace(host, devices=tuple(resolved), volumes=volumes)


def save_storage_identity(host: PhysicalHost, devices: list[dict[str, object]],
                          volumes: dict[str, dict[str, object]]) -> None:
    """Атомарно записать итог после независимой проверки трёх томов."""

    path = storage_state_path(host)
    if path.exists():
        raise InfraManagerError("Назначение NAS уже сохранено; перезапись запрещена")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{host.name}-storage-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({
                "name": host.name, "address": host.address,
                "fingerprint": host.ssh_host_fingerprint,
                "devices": devices, "volumes": volumes,
            }, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)

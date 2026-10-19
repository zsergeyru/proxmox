"""Единое чтение описаний дисков физических машин.

Только проверка YAML и связей. Не обращается к дискам и не разрешает запись.
Существующие обработчики PVE и NAS переключаются на этот формат отдельно.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from .common import InfraManagerError


_HOST_NAME = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class PhysicalStorage:
    """Проверенное требуемое размещение без утверждения о реальном состоянии."""

    host: str
    manifest: Path
    devices: tuple[dict[str, Any], ...]
    volumes: tuple[dict[str, Any], ...]
    mounts: tuple[dict[str, Any], ...]
    policies: dict[str, str]

    def volume(self, name: str) -> dict[str, Any]:
        """Найти объявленный том только по его идентификатору."""
        for volume in self.volumes:
            if volume["id"] == name:
                return volume
        raise InfraManagerError(f"{self.manifest}: неизвестный том {name!r}")


def storage_path(repo_root: Path, host: str) -> Path:
    """Выбрать единственное предусмотренное место файла без обхода каталогов."""

    if not _HOST_NAME.fullmatch(host):
        raise InfraManagerError(f"Недопустимое имя физической машины: {host!r}")
    if host == "pve":
        return repo_root / "infrastructure" / "host" / "pve" / "storage.yaml"
    return repo_root / "infrastructure" / "hosts" / host / "storage.yaml"


def _read_yaml(path: Path) -> Any:
    if path.is_symlink():
        raise InfraManagerError(f"Ссылка вместо машинного файла запрещена: {path}")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise InfraManagerError(f"Нельзя прочитать {path}: {exc}") from exc


def _unique(entries: list[dict[str, Any]], field: str, label: str) -> None:
    values = [entry[field] for entry in entries if entry.get(field) is not None]
    if len(values) != len(set(values)):
        raise InfraManagerError(f"Повторяются {label}: {field}")


def validate_storage(
    repo_root: Path, host: str, document: Any, *, manifest: Path | None = None,
) -> PhysicalStorage:
    """Проверить схему и перекрёстные ссылки без изменения реальных дисков."""

    path = manifest or storage_path(repo_root, host)
    schema_path = repo_root / "infrastructure" / "schemas" / "physical-storage.schema.yaml"
    schema = _read_yaml(schema_path)
    try:
        Draft202012Validator.check_schema(schema)
        errors = sorted(
            Draft202012Validator(schema).iter_errors(document),
            key=lambda error: tuple(str(part) for part in error.path),
        )
    except (ValueError, TypeError) as exc:
        raise InfraManagerError(f"Некорректная схема хранилищ: {schema_path}") from exc
    if errors:
        error = errors[0]
        field = ".".join(str(part) for part in error.path) or "корень"
        raise InfraManagerError(f"{path}: поле {field}: {error.message}")
    if document["host"] != host:
        raise InfraManagerError(f"{path}: имя машины не совпадает с {host}")
    devices = document["devices"]
    volumes = document["volumes"]
    mounts = document["mounts"]
    if sum(device["role"] == "system" for device in devices) != 1:
        raise InfraManagerError(f"{path}: нужен ровно один системный диск")
    for field in ("id", "serial", "by_id"):
        _unique(devices, field, "идентификаторы дисков")
    _unique(volumes, "id", "имена томов")
    _unique(
        [item for item in volumes if item["kind"] == "zfs_pool"],
        "pool", "имена пулов ZFS",
    )
    _unique(volumes, "filesystem_uuid", "UUID файловых систем")
    _unique(volumes, "pool_guid", "GUID пулов ZFS")
    _unique(volumes, "omv_reference", "идентификаторы OMV")
    _unique(mounts, "volume", "подключения одного тома")
    _unique(mounts, "path", "пути подключения")
    for device in devices:
        by_id = device["by_id"]
        if by_id is not None and by_id.rsplit("/", 1)[-1] in {".", ".."}:
            raise InfraManagerError(
                f"{path}: недопустимый постоянный путь диска {device['id']}"
            )
    device_by_id = {device["id"]: device for device in devices}
    volume_by_id = {volume["id"]: volume for volume in volumes}
    direct_devices: set[str] = set()
    for volume in volumes:
        device_id = volume["device"]
        if device_id not in device_by_id:
            raise InfraManagerError(
                f"{path}: том {volume['id']} ссылается на неизвестный диск {device_id}"
            )
        if volume["kind"] != "lvm_thin":
            if device_by_id[device_id]["role"] == "system":
                raise InfraManagerError(
                    f"{path}: запрещено назначать системный диск прямому тому"
                )
            direct_devices.add(device_id)
    for device_id in direct_devices:
        if sum(volume["device"] == device_id for volume in volumes) > 1:
            raise InfraManagerError(
                f"{path}: несколько томов назначены одному физическому диску {device_id}"
            )
    paths: list[str] = []
    for mount in mounts:
        name = mount["volume"]
        if name not in volume_by_id:
            raise InfraManagerError(f"{path}: подключение неизвестного тома {name}")
        if mount["path"] is not None:
            mount_path = mount["path"]
            if any(part in {".", ".."} for part in Path(mount_path).parts):
                raise InfraManagerError(
                    f"{path}: относительные элементы точки подключения запрещены"
                )
            paths.append(mount_path)
    for index, left in enumerate(paths):
        if any(
            left.startswith(right + "/") or right.startswith(left + "/")
            for right in paths[index + 1:]
        ):
            raise InfraManagerError(f"{path}: вложенные точки подключения запрещены")
    if set(volume_by_id) != {item["volume"] for item in mounts}:
        raise InfraManagerError(f"{path}: каждый том должен иметь одно подключение")
    return PhysicalStorage(
        host=host, manifest=path, devices=tuple(devices), volumes=tuple(volumes),
        mounts=tuple(mounts), policies=document["policies"],
    )


def load_physical_storage(repo_root: Path, host: str) -> PhysicalStorage:
    """Загрузить описание машины. Отсутствие файла является ошибкой."""

    path = storage_path(repo_root, host)
    return validate_storage(repo_root, host, _read_yaml(path), manifest=path)


def require_declared_identifiers(storage: PhysicalStorage, *, omv: bool = False) -> None:
    """Отказать при незаполненных идентификаторах.

    Это лишь проверка декларации. Исполнитель отдельно сверяет устройства,
    файловые системы и подключения с фактическим оборудованием.
    """

    devices = {device["id"]: device for device in storage.devices}
    mounts = {mount["volume"]: mount["path"] for mount in storage.mounts}
    for volume in storage.volumes:
        name = volume["id"]
        device = devices[volume["device"]]
        if device["serial"] is None or device["by_id"] is None:
            raise InfraManagerError(f"{storage.manifest}: диск тома {name} не опознан")
        if mounts[name] is None:
            raise InfraManagerError(f"{storage.manifest}: не указан путь тома {name}")
        if volume["kind"] == "zfs_pool":
            identifier = volume["pool_guid"]
        else:
            identifier = volume["filesystem_uuid"]
        if identifier is None:
            raise InfraManagerError(
                f"{storage.manifest}: отсутствует UUID/GUID тома {name}"
            )
        if omv and volume["kind"] != "lvm_thin" and volume.get("omv_reference") is None:
            raise InfraManagerError(
                f"{storage.manifest}: отсутствует идентификатор OMV тома {name}"
            )

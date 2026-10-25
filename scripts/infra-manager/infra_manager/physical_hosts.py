"""Каталог физических машин, независимых от PVE VMID и OpenTofu."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from .common import InfraManagerError
from .storage_catalog import load_physical_storage


_NAME = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class PhysicalHost:
    """Проверенное машинное описание физического сервера."""

    name: str
    address: str | None
    port: int
    user: str
    ssh_host_fingerprint: str | None
    driver: str
    devices: tuple[dict[str, object], ...]
    directory: Path
    volumes: dict[str, dict[str, object]] | None = None


def _validate_physical_layout(storage: object) -> None:
    """Fail closed on contradictory physical layout before any SSH action.

    Only schema and identity relations live in 910. The desired filesystem
    types and hardware operations are interpreted by the host's Ansible role.
    """
    devices = {item["id"]: item for item in storage.devices}
    assigned = [item["role"] for item in storage.devices
                if item["role"] != "unused"]
    if len(assigned) != len(set(assigned)):
        raise InfraManagerError("Одна роль назначена нескольким физическим дискам")
    volumes = {item["id"]: item for item in storage.volumes}
    roles = {item["role"] for item in storage.devices
             if item["role"] not in {"system", "unused"}}
    if set(volumes) != roles:
        raise InfraManagerError("Назначения томов не совпадают с ролями накопителей")
    for item in storage.devices:
        if item["by_id"] is not None and item["serial"] is None:
            raise InfraManagerError(
                "Недопустимый постоянный путь для неподтверждённого HDD"
            )
    for name, volume in volumes.items():
        if volume["device"] != name or name not in devices:
            raise InfraManagerError("Роль тома не соответствует физическому диску")
        if devices[name]["role"] != name:
            raise InfraManagerError("Том ссылается на диск другой роли")
        if volume["kind"] not in {"ext4", "zfs_pool"}:
            raise InfraManagerError("Неподдерживаемая прямая файловая система")
    for item in storage.mounts:
        if item["volume"] not in volumes:
            raise InfraManagerError("Точка подключения неизвестного тома")



def load_physical_host(repo_root: Path, name: str) -> PhysicalHost:
    """Прочитать host.yaml, проверить схему и не допустить обход каталога."""

    if not _NAME.fullmatch(name):
        raise InfraManagerError(f"Некорректное имя физического сервера: {name!r}")

    directory = repo_root / "infrastructure" / "hosts" / name
    manifest = directory / "host.yaml"
    schema_file = repo_root / "infrastructure" / "schemas" / "physical-host.schema.yaml"
    if not manifest.is_file():
        raise InfraManagerError(f"Нет машинного описания физического сервера: {manifest}")
    try:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        schema = yaml.safe_load(schema_file.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise InfraManagerError(f"Не удалось прочитать описание сервера {name}") from exc
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda err: str(err.path))
    if errors:
        error = errors[0]
        location = ".".join(str(item) for item in error.path) or "корень"
        raise InfraManagerError(
            f"{manifest}: ошибка поля {location}: {error.message}"
        )
    if data["name"] != name:
        raise InfraManagerError(f"{manifest}: name не совпадает с каталогом {name}")
    # Диски и тома принадлежат только storage.yaml; host.yaml описывает
    # систему и связь с ней. Не поддерживать конкурирующий источник данных.
    storage = load_physical_storage(repo_root, name)
    driver = data["management"]["driver"]
    # Generic storage safety is independent of any dynamically loaded driver.
    # The Ansible role will additionally verify the actual hardware/UUIDs.
    _validate_physical_layout(storage)

    # Generic conversion from verified storage.yaml into runtime disk metadata.
    devices = tuple({
        "role": entry["role"], "model": entry["model"],
        "capacity_gb": entry["capacity_gb"], "serial": entry["serial"],
    } for entry in storage.devices)
    mounts = {entry["volume"]: entry["path"] for entry in storage.mounts}
    volumes: dict[str, dict[str, object]] = {}
    for entry in storage.volumes:
        name_of_volume = entry["id"]
        volume: dict[str, object] = {"mountpoint": mounts[name_of_volume]}
        if entry["kind"] == "ext4":
            volume.update({
                "filesystem_uuid": entry["filesystem_uuid"],
                "omv_reference": entry.get("omv_reference"),
            })
        elif entry["kind"] == "zfs_pool":
            volume.update({
                "pool": entry["pool"],
                "pool_guid": entry["pool_guid"],
                "omv_reference": entry.get("omv_reference"),
            })
        else:
            raise InfraManagerError(
                f"{storage.manifest}: неподдерживаемый тип файловой системы"
            )
        volumes[name_of_volume] = volume
    # Незаполненные серийные номера допустимы для первоначального плана.
    # Постоянная идентичность потом подтверждается планом и сохранённым состоянием.
    conn = data["connection"]
    return PhysicalHost(
        name=name,
        address=conn["address"],
        port=conn["port"],
        user=conn["user"],
        ssh_host_fingerprint=conn["ssh_host_fingerprint"],
        driver=driver,
        devices=devices,
        directory=directory,
        volumes=volumes,
    )


def list_physical_hosts(repo_root: Path) -> tuple[PhysicalHost, ...]:
    """Вернуть физические серверы с проверенным описанием."""

    root = repo_root / "infrastructure" / "hosts"
    if not root.is_dir():
        return ()
    results: list[PhysicalHost] = []
    for directory in sorted(root.iterdir()):
        if directory.is_dir() and (directory / "host.yaml").is_file():
            results.append(load_physical_host(repo_root, directory.name))
    return tuple(results)

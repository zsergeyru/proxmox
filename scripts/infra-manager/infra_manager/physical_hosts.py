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
    roles = [entry["role"] for entry in storage.devices]
    if driver == "omv8":
        # Исполнитель сейчас независимо выбирает /dev/disk/by-id по серийному
        # номеру с NAS. Заданное в Git значение by_id пока не проверяется
        # им на оборудовании; запрещаем делать вид, что оно проверено.
        if any(entry["by_id"] is not None for entry in storage.devices):
            raise InfraManagerError(
                f"{storage.manifest}: закрепление by_id будет поддержано "
                "только после проверки его исполнителем NAS; пока укажите null"
            )
        for role in ("shared", "data", "backup"):
            if roles.count(role) != 1:
                raise InfraManagerError(
                    f"{storage.manifest}: NAS требует ровно один диск роли {role}"
                )
        if {entry["id"] for entry in storage.volumes} != {
            "shared", "data", "backup"
        }:
            raise InfraManagerError(
                f"{storage.manifest}: NAS требует тома shared, data и backup"
            )

    # Пока исполнитель NAS использует проверенный прежний внутренний
    # контракт, преобразование делается только здесь. Дублирования в Git нет.
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
        elif driver == "omv8":
            raise InfraManagerError(
                f"{storage.manifest}: LVM не используется для томов NAS"
            )
        volumes[name_of_volume] = volume
    if driver == "omv8":
        expected = {"shared": "ext4", "data": "zfs_pool", "backup": "zfs_pool"}
        kinds = {entry["id"]: entry["kind"] for entry in storage.volumes}
        if kinds != expected:
            raise InfraManagerError(
                f"{storage.manifest}: неверный тип файловой системы NAS"
            )
        for entry in storage.volumes:
            if entry["device"] != entry["id"]:
                raise InfraManagerError(
                    f"{storage.manifest}: том NAS назначен чужой роли накопителя"
                )
        for role in ("data", "backup"):
            if volumes[role]["pool"] != "nas-" + role:
                raise InfraManagerError(
                    f"{storage.manifest}: некорректное имя ZFS-пула {role}"
                )
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
